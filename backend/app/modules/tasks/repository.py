from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
import os
import socket

from sqlalchemy import func, select, update
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
        if "allowance" not in task.input_payload:
            return
        owned = False
        if task.status == TaskStatus.completed and task.result_payload:
            fields = [(name, task.result_payload[name]) for name in ("answer_id", "result_id", "file_id", "transcript_id") if name in task.result_payload]
            if fields:
                name, value = fields[0]
                from sqlalchemy import or_
                reference = Task.result_payload[name].as_string() == str(value)
                if name == "result_id":
                    reference = or_(reference, Task.result_payload["summary_result_id"].as_string() == str(value))
                owned = session.scalar(select(Task.id).where(Task.user_id == task.user_id,
                    Task.id != task.id, Task.status == TaskStatus.completed,
                    reference).limit(1)) is not None
        if task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.expired}:
            self._billing().finalize(session, record(task), task.status == TaskStatus.completed, owned)

    def create(self, task_type: str, video_id: UUID | None = None, input_payload: dict | None = None,
               expires_at: datetime | None = None, user_id: UUID | None = None) -> TaskRecord:
        with self.sessions.begin() as session:
            quote = None
            if settings.ALLOWANCES_ENABLED and task_type != "simulate_progress":
                quote = self._billing().bind_quote(session, user_id, input_payload or {}, task_type)
                if quote.task_id:
                    return record(session.get(Task, quote.task_id))
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
            if quote:
                quote.task_id = task.id
            return record(task)

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
        if field not in {"file_id", "transcript_id", "result_id"}:
            raise ValueError("unsupported result reference")
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

    def start(self, task_id: UUID) -> bool:
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
            if settings.ALLOWANCES_ENABLED and task.task_type != "simulate_progress":
                reason = self._billing().admit(session, record(task))
                if reason:
                    task.wait_reason = reason
                    return False
                task.input_payload = {**task.input_payload, "allowance": {**task.input_payload["allowance"],
                    "limits": self._billing().execution_limits(session, task.id)}}
            task.wait_reason = None
            task.status, task.started_at = TaskStatus.running, utcnow()
            task.worker_pid, task.worker_host = os.getpid(), socket.gethostname()
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
            self._lock_task(session, task_id)
            # Check the status in UPDATE, never rely on a prior SELECT.
            session.execute(update(Task).where(Task.id == task_id, Task.status == TaskStatus.queued).values(
                status=TaskStatus.cancelled, cancel_requested=True, finished_at=utcnow(),
                error_code="TASK_CANCELLED", error_message="任务已取消"))
            session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_((TaskStatus.running, TaskStatus.processing))
            ).values(cancel_requested=True))
            session.flush()
            task = session.get(Task, task_id, populate_existing=True)
            self._settle(session, task)
        return self.get(task_id)

    def finish(self, task_id: UUID, status: str, progress: int, result_payload: dict | None = None,
               error_code: str | None = None, error_message: str | None = None) -> TaskRecord:
        if status not in {"completed", "failed", "cancelled", "expired"}:
            raise ValueError("finish requires a terminal status")
        with self.sessions.begin() as session:
            self._lock_task(session, task_id)
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
            self._settle(session, session.get(Task, task_id, populate_existing=True))
        return self.get(task_id)

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
                    session.execute(update(Task).where(Task.id == task_id).values(result_payload=result))
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
