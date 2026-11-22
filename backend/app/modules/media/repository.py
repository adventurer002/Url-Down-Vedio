from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.modules.media.models import MediaFile


@dataclass(frozen=True)
class MediaRecord:
    id: UUID
    task_id: UUID
    video_id: UUID
    preset_id: str
    storage_key: str
    filename: str
    mime_type: str
    size_bytes: int
    expires_at: datetime
    purpose: str = "legacy"
    source_sha256: str | None = None
    source_start: float | None = None
    source_end: float | None = None
    download_key: str | None = None
    object_store_tag: str | None = None


class MediaRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def register(self, session: Session, record: MediaRecord) -> None:
        """Participate in the task completion transaction through this contract."""
        session.add(MediaFile(**record.__dict__))
        session.flush()

    def admin_statistics(self, session, instant):
        count, size, expired = session.execute(select(func.count(), func.coalesce(func.sum(MediaFile.size_bytes), 0),
            func.coalesce(func.sum(case((MediaFile.expires_at <= instant, 1), else_=0)), 0))).one()
        return {"registered_files": count, "registered_bytes": size, "expired_files": expired,
                "note": "数据库登记量，不包含临时文件或孤立文件，不能作为磁盘实测占用"}

    def get(self, file_id: UUID) -> MediaRecord:
        with self.sessions() as session:
            item = session.get(MediaFile, file_id)
            if item is None:
                raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
            return MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})

    def expired(self, now: datetime) -> list[MediaRecord]:
        with self.sessions() as session:
            return [MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})
                    for item in session.scalars(select(MediaFile).where(MediaFile.expires_at <= now))]

    def delete_expired(self, file_id: UUID, now: datetime) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(MediaFile).where(MediaFile.id == file_id, MediaFile.expires_at <= now))

    def source_keys_for_video(self, video_id) -> set[str]:
        with self.sessions() as session:
            return set(session.scalars(select(MediaFile.storage_key).where(MediaFile.video_id == video_id,
                MediaFile.purpose.in_(("legacy", "full_source", "full_audio")))))

    def storage_keys(self) -> set[str]:
        with self.sessions() as session:
            return set(session.scalars(select(MediaFile.storage_key)))

    @staticmethod
    def available_ids_query(instant):
        return select(MediaFile.id).where(MediaFile.expires_at > instant, MediaFile.purpose == "clip")

    def require_available(self, session, file_ids, instant):
        if not file_ids:
            return
        found = set(session.scalars(select(MediaFile.id).where(MediaFile.id.in_(file_ids),
            MediaFile.expires_at > instant, MediaFile.purpose == "clip").with_for_update()))
        if found != set(file_ids):
            raise AppException("CLIP_REQUOTE_REQUIRED", "复用成片已过期，请重新报价", 409)

    def available(self, file_ids: list[UUID], now: datetime) -> list[MediaRecord]:
        with self.sessions() as session:
            return [MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})
                    for item in session.scalars(select(MediaFile).where(MediaFile.id.in_(file_ids), MediaFile.expires_at > now))]

    def reusable(self, video_id: UUID, now: datetime) -> list[MediaRecord]:
        with self.sessions() as session:
            return [MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})
                    for item in session.scalars(select(MediaFile).where(
                        MediaFile.video_id == video_id, MediaFile.expires_at > now,
                        MediaFile.purpose.in_(("legacy", "full_source", "full_audio"))
                    ).order_by(MediaFile.created_at.desc()))]

    def find_download(self, key, now, file_limit):
        with self.sessions() as session:
            item = session.scalar(select(MediaFile).where(MediaFile.download_key == key,
                MediaFile.expires_at > now, MediaFile.size_bytes <= file_limit
            ).order_by(MediaFile.created_at.desc()).limit(1))
            if item:
                return MediaRecord(**{field: getattr(item, field) for field in MediaRecord.__dataclass_fields__})

    @staticmethod
    def mirror_conditions(now):
        return (
            MediaFile.object_store_tag.is_(None), MediaFile.purpose.in_(("full_source", "full_audio")),
            MediaFile.expires_at > now, MediaFile.object_attempts < 3,
            or_(MediaFile.object_attempted_at.is_(None), MediaFile.object_attempted_at <= now - timedelta(minutes=1)),
        )

    def next_mirror(self, now):
        with self.sessions() as session:
            return session.scalar(select(MediaFile.id).where(*self.mirror_conditions(now))
                                  .order_by(MediaFile.created_at.desc()).limit(1))

    def begin_mirror(self, file_id, now):
        with self.sessions.begin() as session:
            return bool(session.execute(update(MediaFile).where(MediaFile.id == file_id,
                *self.mirror_conditions(now)).values(object_attempted_at=now,
                    object_attempts=MediaFile.object_attempts + 1)).rowcount)

    def finish_mirror(self, file_id, tag, now):
        with self.sessions.begin() as session:
            return bool(session.execute(update(MediaFile).where(MediaFile.id == file_id,
                MediaFile.object_store_tag.is_(None), MediaFile.expires_at > now)
                .values(object_store_tag=tag)).rowcount)
