from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import AppException
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


def record(task: Task) -> TaskRecord:
    return TaskRecord(**{name: (task.status.value if name == "status" else getattr(task, name))
                         for name in TaskRecord.__dataclass_fields__})


class TaskRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def create(self, task_type: str, video_id: UUID | None = None, input_payload: dict | None = None,
               expires_at: datetime | None = None, user_id: UUID | None = None) -> TaskRecord:
        with self.sessions.begin() as session:
            task = Task(user_id=user_id, task_type=task_type, video_id=video_id, input_payload=input_payload or {}, expires_at=expires_at)
            session.add(task)
            session.flush()
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
            owners = [Task.user_id.is_(None)]
            if user_id is not None:
                owners.append(Task.user_id == user_id)
            return session.scalar(select(Task.id).where(
                Task.status == TaskStatus.completed,
                Task.result_payload[field].as_string().in_([str(item) for item in result_ids]), or_(*owners)
            ).limit(1)) is not None

    def get(self, task_id: UUID) -> TaskRecord:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise AppException("TASK_NOT_FOUND", "任务不存在", 404)
            return record(task)

    def start(self, task_id: UUID) -> bool:
        with self.sessions.begin() as session:
            session.execute(update(Task).where(
                Task.id == task_id, Task.status == TaskStatus.queued, Task.expires_at <= utcnow()
            ).values(status=TaskStatus.expired, error_code="TASK_EXPIRED", error_message="任务已过期", finished_at=utcnow()))
            changed = session.execute(update(Task).where(
                Task.id == task_id, Task.status == TaskStatus.queued, Task.cancel_requested.is_(False)
            ).values(status=TaskStatus.running, started_at=utcnow(), attempts=Task.attempts + 1)).rowcount
            return bool(changed)

    def mark_processing(self, task_id: UUID) -> bool:
        with self.sessions.begin() as session:
            return bool(session.execute(update(Task).where(
                Task.id == task_id, Task.status == TaskStatus.running, Task.cancel_requested.is_(False)
            ).values(status=TaskStatus.processing)).rowcount)

    def request_cancel(self, task_id: UUID) -> TaskRecord:
        with self.sessions.begin() as session:
            # Check the status in UPDATE, never rely on a prior SELECT.
            session.execute(update(Task).where(Task.id == task_id, Task.status == TaskStatus.queued).values(
                status=TaskStatus.cancelled, cancel_requested=True, finished_at=utcnow(),
                error_code="TASK_CANCELLED", error_message="任务已取消"))
            session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_((TaskStatus.running, TaskStatus.processing))
            ).values(cancel_requested=True))
        return self.get(task_id)

    def finish(self, task_id: UUID, status: str, progress: int, result_payload: dict | None = None,
               error_code: str | None = None, error_message: str | None = None) -> TaskRecord:
        if status not in {"completed", "failed", "cancelled", "expired"}:
            raise ValueError("finish requires a terminal status")
        with self.sessions.begin() as session:
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
        return self.get(task_id)

    def perform_if_active(self, task_id: UUID, writer):
        """Serialize intermediate module writes against cancellation without completing the parent."""
        with self.sessions.begin() as session:
            task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.status not in (TaskStatus.running, TaskStatus.processing) or task.cancel_requested:
                raise AppException("TASK_CANCELLED", "任务已取消或终止", 409)
            return writer(session)

    def complete_with(self, task_id: UUID, result_payload: dict, write_result) -> TaskRecord:
        """CAS and module-owned result registration share one database transaction."""
        with self.sessions.begin() as session:
            changed = session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_((TaskStatus.running, TaskStatus.processing)),
                Task.cancel_requested.is_(False)
            ).values(status=TaskStatus.completed, progress=100, result_payload=result_payload,
                     finished_at=utcnow(), error_code=None, error_message=None)).rowcount
            if changed:
                result = write_result(session)
                if isinstance(result, dict):
                    session.execute(update(Task).where(Task.id == task_id).values(result_payload=result))
        task = self.get(task_id)
        if task.status in {"running", "processing"} and task.cancel_requested:
            task = self.finish(task_id, "cancelled", task.progress, error_code="TASK_CANCELLED", error_message="任务已取消")
        return task

    def expire_stale(self, task_id: UUID) -> TaskRecord:
        with self.sessions.begin() as session:
            session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_(ACTIVE_STATUSES), Task.expires_at <= utcnow()
            ).values(status=TaskStatus.expired, error_code="TASK_EXPIRED", error_message="任务已过期", finished_at=utcnow()))
        return self.get(task_id)
