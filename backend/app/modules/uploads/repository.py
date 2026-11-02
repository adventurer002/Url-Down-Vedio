from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.billing.policy import aware, now
from app.modules.billing.repository import AllowanceRepository
from app.modules.uploads.models import Upload


def record(item):
    return {name: getattr(item, name) for name in ("id", "user_id", "filename", "extension", "expected_bytes",
        "size_bytes", "content_sha256", "status", "video_id", "task_id", "duration_seconds", "media_type",
        "error_code", "created_at", "expires_at")}


class UploadRepository:
    def ready_for_video(self, user_id, video_id):
        with self.sessions() as session:
            return session.scalar(select(Upload.id).where(Upload.user_id == user_id, Upload.video_id == video_id,
                Upload.status == "ready", Upload.expires_at > now()).order_by(Upload.created_at.desc()).limit(1))

    def __init__(self, sessions):
        self.sessions = sessions

    def limits(self, session, user_id):
        billing = AllowanceRepository(self.sessions)
        account, _ = billing.lock_account(session, user_id)
        return billing.limits(account)

    def resource_limits(self, user_id):
        with self.sessions.begin() as session:
            return self.limits(session, user_id)

    def create(self, identifier, user_id, filename, extension, size):
        with self.sessions.begin() as session:
            limits = self.limits(session, user_id)
            if size > min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2):
                raise AppException("FILE_TOO_LARGE", "文件超过当前账号的上传体积上限", 413)
            count = session.scalar(select(func.count()).select_from(Upload).where(Upload.user_id == user_id,
                Upload.status.in_(["pending", "uploaded", "validating"]), Upload.expires_at > now()))
            if count >= settings.UPLOAD_MAX_PENDING_PER_USER:
                raise AppException("TOO_MANY_UPLOADS", "未完成的上传过多，请先处理或删除", 429)
            recent = session.scalar(select(func.count()).select_from(Upload).where(Upload.user_id == user_id,
                Upload.created_at > now() - timedelta(hours=1)))
            if recent >= settings.UPLOAD_MAX_PER_HOUR:
                raise AppException("UPLOAD_RATE_LIMITED", "上传过于频繁，请稍后再试", 429)
            item = Upload(id=identifier, user_id=user_id, filename=filename, extension=extension,
                expected_bytes=size, expires_at=now() + timedelta(seconds=settings.UPLOAD_SESSION_TTL_SECONDS))
            session.add(item)
            session.flush()
            return record(item)

    def owned(self, session, identifier, user_id, *, lock=False):
        query = select(Upload).where(Upload.id == identifier, Upload.user_id == user_id)
        if lock:
            query = query.with_for_update()
        item = session.scalar(query)
        if item is None:
            raise AppException("UPLOAD_NOT_FOUND", "上传记录不存在", 404)
        return item

    def get(self, identifier, user_id):
        with self.sessions() as session:
            return record(self.owned(session, identifier, user_id))

    @staticmethod
    def available(item, statuses):
        if item.status in {"expired", "deleted"} or aware(item.expires_at) <= now():
            raise AppException("UPLOAD_EXPIRED", "文件已过期或删除，请重新上传", 410)
        if item.status not in statuses:
            raise AppException("UPLOAD_NOT_READY", "文件尚未就绪或校验失败", 409)

    def received(self, identifier, user_id, size, digest):
        with self.sessions.begin() as session:
            item = self.owned(session, identifier, user_id, lock=True)
            self.available(item, {"pending"})
            item.size_bytes, item.content_sha256, item.status = size, digest, "uploaded"
            item.expires_at = now() + timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)
            session.flush()
            return record(item)

    def bind_probe(self, session, identifier, user_id, task_id=None):
        item = self.owned(session, identifier, user_id, lock=True)
        self.available(item, {"uploaded", "validating", "ready", "failed"})
        if item.task_id:
            return item.task_id
        if item.status != "uploaded":
            raise AppException("UPLOAD_NOT_READY", "请先上传文件内容", 409)
        if task_id is not None:
            item.task_id, item.status = task_id, "validating"
        return None

    def ready(self, session, identifier, user_id, task_id, duration_seconds, media_type):
        item = self.owned(session, identifier, user_id, lock=True)
        self.available(item, {"validating"})
        if item.task_id != task_id:
            raise AppException("UPLOAD_TASK_MISMATCH", "文件校验任务不匹配", 409)
        from app.modules.video.repository import VideoRepository
        item.video_id = VideoRepository(self.sessions).register_upload(session, user_id, item.content_sha256,
            item.filename, duration_seconds, media_type)
        item.duration_seconds, item.media_type, item.status = duration_seconds, media_type, "ready"
        item.expires_at = now() + timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)
        session.flush()
        return {"upload_id": str(item.id), "video_id": str(item.video_id), "media_type": media_type,
                "duration_seconds": duration_seconds}

    def terminal(self, identifier, user_id, status, error_code=None):
        with self.sessions.begin() as session:
            item = self.owned(session, identifier, user_id, lock=True)
            if item.status not in {"deleted", "expired"}:
                item.status, item.error_code = status, error_code
            session.flush()
            return record(item)

    def cleanup_candidates(self, instant, limit=100, after=None):
        with self.sessions() as session:
            conditions = [Upload.purged_at.is_(None), Upload.created_at <= instant,
                or_(Upload.expires_at <= instant, Upload.status.in_(["deleted", "expired", "failed"]))]
            if after is not None:
                expiry, identifier = after
                conditions.append(or_(Upload.expires_at > expiry,
                    and_(Upload.expires_at == expiry, Upload.id > identifier)))
            return [record(x) for x in session.scalars(select(Upload).where(*conditions)
                .order_by(Upload.expires_at, Upload.id).limit(limit))]

    def claim_cleanup(self, identifier, user_id, instant):
        # Recheck under the source lock: a probe may have renewed the expiry since the page was read.
        with self.sessions.begin() as session:
            item = self.owned(session, identifier, user_id, lock=True)
            terminal = item.status in {"deleted", "expired", "failed"}
            if item.purged_at is not None or (not terminal and aware(item.expires_at) > instant):
                return False
            if not terminal:
                item.status = "expired"
            return True

    def mark_purged(self, identifier, user_id):
        with self.sessions.begin() as session:
            item = self.owned(session, identifier, user_id, lock=True)
            item.purged_at = now()

    def storage_ids(self):
        with self.sessions() as session:
            return {str(x) for x in session.scalars(select(Upload.id))}

    def pending_reservation_ids(self):
        with self.sessions() as session:
            return {str(x) for x in session.scalars(select(Upload.id).where(
                Upload.status == "pending", Upload.expires_at > now()))}
