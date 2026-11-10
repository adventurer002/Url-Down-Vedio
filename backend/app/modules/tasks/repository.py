from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
import os
import socket

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import AppException
from app.core.config import settings
from app.modules.tasks.models import ACTIVE_STATUSES, Task, TaskStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TaskRecord:
    id: UUID
    video_id: UUID | None
    task_type: str
    status: str
    progress: int
    input_payload: dict
    result_payload: dict | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    user_id: UUID | None = None
    wait_reason: str | None = None
    worker_pid: int | None = None
    worker_host: str | None = None


def record(task: Task) -> TaskRecord:
    return TaskRecord(**{name: (task.status.value if name == "status" else getattr(task, name))
                         for name in TaskRecord.__dataclass_fields__})


class TaskRepository:
    @staticmethod
    def completed_owned_ids(user_id):
        """Query contract for module-owned artifacts; callers need not access Task models."""
        return select(Task.id).where(Task.user_id == user_id, Task.status == TaskStatus.completed)

    def clip_render_counts(self, session, user_id, plan_id):
        from app.modules.billing.policy import BEIJING
        start = utcnow().astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        mode = Task.input_payload["_clip_execution_mode"].as_string()
        legacy_render = Task.input_payload["reuse_result"]["clip_export_id"].as_string().is_(None)
        rendered = or_(mode.in_(("reserved", "render")), mode.is_(None) & legacy_render)
        total, plan = session.execute(select(func.count(), func.coalesce(func.sum(case(
            (Task.input_payload["plan_id"].as_string() == str(plan_id), 1), else_=0)), 0)).where(
                Task.user_id == user_id, Task.task_type == "clips_export", Task.started_at >= start, rendered)).one()
        return total, plan

    def clip_execution_mode(self, task_id, mode):
        if mode not in {"cache", "render"}:
            raise ValueError("invalid clip execution mode")
        with self.sessions.begin() as session:
            task = self._lock_task(session, task_id)
            if task.status not in (TaskStatus.running, TaskStatus.processing) or task.cancel_requested:
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            previous = task.input_payload.get("_clip_execution_mode")
            if previous == "render" and mode != "render":
                raise ValueError("real work cannot be refunded as cache")
            if previous == "cache" and mode != "cache":
                raise AppException("CLIP_REQUOTE_REQUIRED", "缓存已失效，请重新报价", 409)
            task.input_payload = {**task.input_payload, "_clip_execution_mode": mode}

    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def _billing(self):
        from app.modules.billing.repository import AllowanceRepository
        return AllowanceRepository(self.sessions)

    def _lock_task(self, session, task_id):
        initial = session.get(Task, task_id)
        if initial is None:
            raise AppException("TASK_NOT_FOUND", "任务不存在", 404)
        if initial.user_id and "allowance" in initial.input_payload:
            self._billing().lock_existing(session, initial.user_id)
        return session.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))

    def _settle(self, session, task):
        if task.task_type == "model_test": return
        if "allowance" not in task.input_payload:
            return
        owned = False
        if task.status == TaskStatus.completed and task.result_payload:
            fields = [(name, task.result_payload[name]) for name in ("clip_export_id", "clip_plan_id", "answer_id", "result_id", "file_id", "transcript_id") if name in task.result_payload]
            if fields:
                name, value = fields[0]
                from sqlalchemy import or_
                reference = Task.result_payload[name].as_string() == str(value)
                if name == "result_id":
                    reference = or_(reference, Task.result_payload["summary_result_id"].as_string() == str(value))
                owned = session.scalar(select(Task.id).where(Task.user_id == task.user_id,
                    Task.id != task.id, Task.status == TaskStatus.completed,
                    reference).limit(1)) is not None
        if task.task_type == "ai_clip_plan" and (task.result_payload or {}).get("outcome") == "no_highlights":
            owned = True  # Explicit no-delivery policy for clips only; release all held allowance.
        if task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.expired}:
            self._billing().finalize(session, record(task), task.status == TaskStatus.completed, owned)

    def create(self, task_type: str, video_id: UUID | None = None, input_payload: dict | None = None,
               expires_at: datetime | None = None, user_id: UUID | None = None) -> TaskRecord:
        with self.sessions.begin() as session:
            quote = None
            probe_upload = None
            model_probe = None
            if task_type == "model_test":
                from app.modules.model_config.service import ModelConfigService
                model_probe = ModelConfigService(self.sessions)
                previous = model_probe.bind_probe(session, input_payload["probe_id"], user_id)
                if previous: return record(session.get(Task,previous))
            elif task_type in {"transcription","ai_summary","ai_mindmap","ai_question","ai_clip_plan","clips_export"}:
                from app.modules.model_config.runtime import capture,validate_snapshot
                input_payload = dict(input_payload or {})
                if "model_config" not in input_payload and not settings.ALLOWANCES_ENABLED:
                    snapshot=capture(session,lock=True)
                    if snapshot: input_payload["model_config"]=snapshot
                if not settings.ALLOWANCES_ENABLED:validate_snapshot(session,input_payload)

            if task_type == "media_probe":
                from app.modules.uploads.repository import UploadRepository
                probe_upload = UploadRepository(self.sessions)
                previous = probe_upload.bind_probe(session, UUID(input_payload["upload_id"]), user_id)
                if previous:
                    return record(session.get(Task, previous))
            if settings.ALLOWANCES_ENABLED and task_type not in {"simulate_progress", "media_probe", "model_test"}:
                quote = self._billing().bind_quote(session, user_id, input_payload or {}, task_type)
                if quote.task_id:
                    return record(session.get(Task, quote.task_id))
                if task_type in {"transcription","ai_summary","ai_mindmap","ai_question","ai_clip_plan","clips_export"}:
                    from app.modules.model_config.runtime import validate_snapshot
                    validate_snapshot(session,input_payload or {})
                if task_type == "clips_export":
                    previous = session.scalar(select(Task.id).where(Task.user_id == user_id,
                        Task.task_type == "clips_export", Task.status.in_(ACTIVE_STATUSES),
                        Task.input_payload["export_key"].as_string() == input_payload["export_key"]).limit(1))
                    if previous:
                        raise AppException("CLIP_EXPORT_IN_PROGRESS", "相同切片导出已在排队或执行中", 409)
                pending = session.scalar(select(func.count()).select_from(Task).where(Task.user_id == user_id, Task.status.in_(ACTIVE_STATUSES)))
                if pending >= 10:
                    raise AppException("TOO_MANY_TASKS", "最多保留 10 个排队或执行中的任务", 429)
                recent = session.scalar(select(func.count()).select_from(Task).where(Task.user_id == user_id,
                    Task.created_at > utcnow() - timedelta(hours=1)))
                if recent >= 60:
                    raise AppException("TASK_RATE_LIMITED", "每小时最多创建 60 个任务，请稍后重试", 429)
            task = Task(user_id=user_id, task_type=task_type, video_id=video_id, input_payload=input_payload or {}, expires_at=expires_at)
            session.add(task)
            session.flush()
            if model_probe:
                model_probe.bind_probe(session,input_payload["probe_id"],user_id,task.id)
            if probe_upload:
                probe_upload.bind_probe(session, UUID(input_payload["upload_id"]), user_id, task.id)
            if quote:
                quote.task_id = task.id
            return record(task)

    def active_model_test_ids(self, session):
        return list(session.scalars(select(Task.id).where(
            Task.task_type == "model_test", Task.status.in_(ACTIVE_STATUSES)
        )))

    def model_probe_results(self,session,task_ids):
        ids=[identifier for identifier in task_ids if identifier]
        if not ids:return {}
        return {task.id:{"status":task.status.value if hasattr(task.status,"value") else task.status,
                "result":task.result_payload or {},"error_code":task.error_code}
                for task in session.scalars(select(Task).where(Task.id.in_(ids),Task.task_type=="model_test"))}

    def model_probe_result(self,session,task_id):
        return self.model_probe_results(session,[task_id]).get(task_id)

    def list_recent(self, limit: int = 20, offset: int = 0, user_id: UUID | None = None) -> tuple[list[TaskRecord], int]:
        with self.sessions() as session:
            condition = [Task.history_hidden.is_(False)]
            if user_id is not None:
                condition.append(Task.user_id == user_id)
            total = session.scalar(select(func.count()).select_from(Task).where(*condition))
            items = session.scalars(select(Task).where(*condition).order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset))
            return [record(item) for item in items], total

    def hide_history(self, user_id: UUID, task_id: UUID | None = None) -> int:
        # History is presentation state. Keep task rows, live work and result grants intact.
        with self.sessions.begin() as session:
            conditions = [Task.user_id == user_id, Task.history_hidden.is_(False)]
            if task_id is not None:
                owned = session.scalar(select(Task.id).where(Task.id == task_id, Task.user_id == user_id))
                if owned is None:
                    raise AppException("TASK_NOT_FOUND", "任务记录不存在", 404)
                conditions.append(Task.id == task_id)
            return session.execute(update(Task).where(*conditions).values(history_hidden=True)).rowcount

    def has_result_grant(self, field: str, result_ids: list[UUID], user_id: UUID | None) -> bool:
        if field not in {"file_id", "transcript_id", "result_id", "clip_plan_id", "clip_export_id"}:
            raise ValueError("unsupported result reference")
        if field == "file_id":
            from app.modules.clips.repository import ClipRepository
            if ClipRepository(self.sessions).can_read_file(user_id, result_ids):
                return True
        from sqlalchemy import or_
        with self.sessions() as session:
            owners = [] if settings.ALLOWANCES_ENABLED else [Task.user_id.is_(None)]
            if user_id is not None:
                owners.append(Task.user_id == user_id)
            reference = Task.result_payload[field].as_string().in_([str(item) for item in result_ids])
            if field == "result_id":
                reference = or_(reference, Task.result_payload["summary_result_id"].as_string().in_([str(item) for item in result_ids]))
            return session.scalar(select(Task.id).where(
                Task.status == TaskStatus.completed,
                reference, or_(*owners) if owners else False
            ).limit(1)) is not None

    def get(self, task_id: UUID) -> TaskRecord:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise AppException("TASK_NOT_FOUND", "任务不存在", 404)
            return record(task)

    def start(self, task_id: UUID, *, execution_id: str | None = None, clip_cache_ready=False) -> bool:
        with self.sessions.begin() as session:
            task = self._lock_task(session, task_id)
            if task.status != TaskStatus.queued or task.cancel_requested:
                return False
            expiry = task.expires_at
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry is not None and expiry <= utcnow():
                task.status, task.finished_at = TaskStatus.expired, utcnow()
                task.error_code, task.error_message = "TASK_EXPIRED", "任务已过期"
                self._settle(session, task)
                return False
            if task.task_type == "model_test":
                from app.modules.auth.repository import UserRepository
                try:UserRepository(self.sessions).require_admin(session,task.user_id,task.input_payload["admin_version"])
                except AppException:
                    task.status,task.finished_at=TaskStatus.failed,utcnow()
                    task.error_code,task.error_message="MODEL_TEST_AUTH_REVOKED","发起测试的管理员权限已失效"
                    return False
            if task.task_type == "clips_export":
                task.input_payload = {**task.input_payload, "_clip_execution_mode": "cache" if clip_cache_ready or task.input_payload.get("reuse_result") or task.input_payload.get("reuse_assets") else "reserved"}
            if settings.ALLOWANCES_ENABLED and task.task_type not in {"simulate_progress", "media_probe", "model_test"}:
                reason = self._billing().admit(session, record(task))
                if reason:
                    task.wait_reason = reason
                    return False
                task.input_payload = {**task.input_payload, "allowance": {**task.input_payload["allowance"],
                    "limits": self._billing().execution_limits(session, task.id)}}
            task.wait_reason = None
            task.status, task.started_at = TaskStatus.running, utcnow()
            task.worker_pid, task.worker_host = os.getpid(), socket.gethostname()
            if execution_id is not None:
                task.input_payload = {**task.input_payload, "_execution_id": execution_id}
            task.attempts += 1
            session.flush()
            return True

    def mark_processing(self, task_id: UUID) -> bool:
        with self.sessions.begin() as session:
            return bool(session.execute(update(Task).where(
                Task.id == task_id, Task.status == TaskStatus.running, Task.cancel_requested.is_(False)
            ).values(status=TaskStatus.processing)).rowcount)

    def request_cancel(self, task_id: UUID) -> TaskRecord:
        with self.sessions.begin() as session:
            return self.request_cancel_in_transaction(session, task_id)

    def request_cancel_in_transaction(self, session, task_id, before_change=None):
        """Same CAS/settlement contract, participating in an administrative audit transaction."""
        original = self._lock_task(session, task_id)
        if before_change:
            before_change(record(original))
        session.execute(update(Task).where(Task.id == task_id, Task.status == TaskStatus.queued).values(
            status=TaskStatus.cancelled, cancel_requested=True, finished_at=utcnow(),
            error_code="TASK_CANCELLED", error_message="任务已取消"))
        session.execute(update(Task).where(
            Task.id == task_id, Task.status.in_((TaskStatus.running, TaskStatus.processing))
        ).values(cancel_requested=True))
        session.flush()
        task = session.get(Task, task_id, populate_existing=True)
        self._settle(session, task)
        return record(task)

    def admin_detail(self, session, task_id):
        task = session.get(Task, task_id, populate_existing=True)
        if task is None:
            raise AppException("TASK_NOT_FOUND", "任务不存在", 404)
        return self.admin_record(task)

    @staticmethod
    def admin_record(task):
        data = {name: getattr(task, name) for name in (
            "user_id", "video_id", "task_type", "progress", "error_code", "wait_reason", "attempts",
            "cancel_requested", "history_hidden", "created_at", "started_at", "finished_at", "expires_at")}
        data.update(task_id=task.id, status=task.status.value)
        references = {}
        for name in ("clip_export_id", "clip_plan_id", "file_id", "transcript_id", "result_id", "summary_result_id", "answer_id"):
            try:
                references[name] = str(UUID((task.result_payload or {})[name]))
            except (KeyError, ValueError, TypeError, AttributeError):
                pass
        data["result_references"] = references
        instant = utcnow()
        aware = lambda value: value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        data["queue_seconds"] = max(0, (aware(task.started_at or task.finished_at or instant) - aware(task.created_at)).total_seconds())
        data["execution_seconds"] = max(0, (aware(task.finished_at or instant) - aware(task.started_at)).total_seconds()) if task.started_at else None
        return data

    def admin_list(self, session, limit, offset, user_id=None, status=None, task_type=None, since=None, until=None):
        conditions = []
        for column, value in ((Task.user_id, user_id), (Task.status, status), (Task.task_type, task_type)):
            if value is not None:
                conditions.append(column == value)
        if since is not None:
            conditions.append(Task.created_at >= since)
        if until is not None:
            conditions.append(Task.created_at < until)
        total = session.scalar(select(func.count()).select_from(Task).where(*conditions))
        items = session.scalars(select(Task).where(*conditions)
            .order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset))
        return {"items": [self.admin_record(item) for item in items], "total": total, "limit": limit, "offset": offset}

    def status_map(self, session, task_ids):
        if not task_ids:
            return {}
        return {str(identifier): status.value for identifier, status in session.execute(
            select(Task.id, Task.status).where(Task.id.in_(task_ids)))}

    def admin_statistics(self, session, day_start):
        counts = {status.value: 0 for status in TaskStatus}
        counts.update({status.value: count for status, count in session.execute(
            select(Task.status, func.count()).group_by(Task.status))})
        return {"by_status": counts, "total": sum(counts.values()),
                "created_today": session.scalar(select(func.count()).select_from(Task).where(Task.created_at >= day_start))}

    def finish(self, task_id: UUID, status: str, progress: int, result_payload: dict | None = None,
               error_code: str | None = None, error_message: str | None = None,
               *, expected_execution_id: str | None = None) -> TaskRecord:
        if status not in {"completed", "failed", "cancelled", "expired"}:
            raise ValueError("finish requires a terminal status")
        with self.sessions.begin() as session:
            current = self._lock_task(session, task_id)
            if expected_execution_id is not None and current.input_payload.get("_execution_id") != expected_execution_id:
                return record(current)
            condition = [Task.id == task_id, Task.status.in_(ACTIVE_STATUSES)]
            if status in {"completed", "failed"}:
                condition.append(Task.cancel_requested.is_(False))
            session.execute(update(Task).where(*condition).values(
                status=TaskStatus(status), progress=100 if status == "completed" else progress,
                finished_at=utcnow(), result_payload=result_payload, error_code=error_code, error_message=error_message))
            # A cancellation that won the CAS takes precedence over completion/failure.
            session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_(ACTIVE_STATUSES), Task.cancel_requested.is_(True)
            ).values(status=TaskStatus.cancelled, progress=progress, finished_at=utcnow(),
                     result_payload=None, error_code="TASK_CANCELLED", error_message="任务已取消"))
            session.flush()
            final = session.get(Task, task_id, populate_existing=True)
            self._settle(session, final)
            result = record(final)
        return result

    def perform_if_active(self, task_id: UUID, writer):
        """Serialize intermediate module writes against cancellation without completing the parent."""
        with self.sessions.begin() as session:
            task = self._lock_task(session, task_id)
            if task is None or task.status not in (TaskStatus.running, TaskStatus.processing) or task.cancel_requested:
                raise AppException("TASK_CANCELLED", "任务已取消或终止", 409)
            return writer(session)

    def complete_with(self, task_id: UUID, result_payload: dict, write_result) -> TaskRecord:
        """CAS and module-owned result registration share one database transaction."""
        with self.sessions.begin() as session:
            self._lock_task(session, task_id)
            changed = session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_((TaskStatus.running, TaskStatus.processing)),
                Task.cancel_requested.is_(False)
            ).values(status=TaskStatus.completed, progress=100, result_payload=result_payload,
                     finished_at=utcnow(), error_code=None, error_message=None)).rowcount
            if changed:
                result = write_result(session)
                if isinstance(result, dict):
                    values = {"result_payload": result}
                    if result.get("video_id") and session.get(Task, task_id).task_type == "media_probe":
                        values["video_id"] = UUID(result["video_id"])
                    session.execute(update(Task).where(Task.id == task_id).values(**values))
                session.flush()
                self._settle(session, session.get(Task, task_id, populate_existing=True))
        task = self.get(task_id)
        if task.status in {"running", "processing"} and task.cancel_requested:
            task = self.finish(task_id, "cancelled", task.progress, error_code="TASK_CANCELLED", error_message="任务已取消")
        return task

    def expire_stale(self, task_id: UUID) -> TaskRecord:
        with self.sessions.begin() as session:
            self._lock_task(session, task_id)
            session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_(ACTIVE_STATUSES), Task.expires_at <= utcnow()
            ).values(status=TaskStatus.expired, error_code="TASK_EXPIRED", error_message="任务已过期", finished_at=utcnow()))
            self._settle(session, session.get(Task, task_id, populate_existing=True))
        return self.get(task_id)

    def record_asr(self, task_id, duration):
        with self.sessions.begin() as session:
            task = self._lock_task(session, task_id)
            if task.cancel_requested or task.status not in {TaskStatus.running, TaskStatus.processing}:
                raise AppException("TASK_CANCELLED", "任务已取消或终止", 409)
            self._billing().record_asr(session, task_id, duration)

    def queued(self, user_id=None, limit=100):
        with self.sessions() as session:
            query = select(Task).where(Task.status == TaskStatus.queued)
            if user_id is not None:
                query = query.where(Task.user_id == user_id)
            return [record(x) for x in session.scalars(query.order_by(Task.last_dispatched_at.asc().nulls_first(), Task.created_at).limit(limit))]

    def mark_dispatched(self, task_id):
        with self.sessions.begin() as session:
            session.execute(update(Task).where(Task.id == task_id, Task.status == TaskStatus.queued)
                            .values(last_dispatched_at=utcnow()))

    def running(self, limit=100):
        with self.sessions() as session:
            return [record(x) for x in session.scalars(select(Task).where(
                Task.status.in_((TaskStatus.running, TaskStatus.processing))).order_by(Task.started_at).limit(limit))]

    def own_ai_result(self, user_id, video_id, task_type, language, model, summary_version, mindmap_version):
        with self.sessions() as session:
            for task in session.scalars(select(Task).where(Task.user_id == user_id,
                    Task.video_id == video_id, Task.task_type.in_([task_type, "ai_mindmap"] if task_type == "ai_summary" else [task_type]),
                    Task.status == TaskStatus.completed)
                    .order_by(Task.finished_at.desc())):
                payload = task.input_payload
                snapshot = payload.get("allowance", {})
                if (payload.get("output_language") == language and snapshot.get("llm_model") == model
                        and snapshot.get("summary_version") == summary_version
                        and snapshot.get("mindmap_version") == mindmap_version
                        and payload.get("content_version") == settings.TRANSCRIPT_CONTENT_VERSION):
                    if task_type == "ai_summary" and task.task_type == "ai_mindmap":
                        result = task.result_payload or {}
                        if result.get("summary_result_id"):
                            return {"result_id": result["summary_result_id"], "result_type": "summary",
                                    "transcript_id": result["transcript_id"], "cache_hit": True}
                    else:
                        return task.result_payload
        return None

    def has_active_upload(self, upload_id):
        from sqlalchemy import or_, and_
        with self.sessions() as session:
            return session.scalar(select(Task.id).where(
                Task.input_payload["upload_id"].as_string() == str(upload_id),
                or_(Task.status.in_((TaskStatus.running, TaskStatus.processing)),
                    and_(Task.status == TaskStatus.queued, or_(Task.expires_at.is_(None), Task.expires_at > utcnow())))
            ).limit(1)) is not None

    def upload_task_ids(self, upload_id):
        with self.sessions() as session:
            return list(session.scalars(select(Task.id).where(
                Task.input_payload["upload_id"].as_string() == str(upload_id))))
