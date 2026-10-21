from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import update
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


def record(task: Task) -> TaskRecord:
    return TaskRecord(**{name: (task.status.value if name == "status" else getattr(task, name))
                         for name in TaskRecord.__dataclass_fields__})


class TaskRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def create(self, task_type: str, video_id: UUID | None = None, input_payload: dict | None = None,
               expires_at: datetime | None = None) -> TaskRecord:
        with self.sessions.begin() as session:
            task = Task(task_type=task_type, video_id=video_id, input_payload=input_payload or {}, expires_at=expires_at)
            session.add(task)
            session.flush()
            return record(task)

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

    def complete_with(self, task_id: UUID, result_payload: dict, write_result) -> TaskRecord:
        """CAS and module-owned result registration share one database transaction."""
        with self.sessions.begin() as session:
            changed = session.execute(update(Task).where(
                Task.id == task_id, Task.status.in_((TaskStatus.running, TaskStatus.processing)),
                Task.cancel_requested.is_(False)
            ).values(status=TaskStatus.completed, progress=100, result_payload=result_payload,
                     finished_at=utcnow(), error_code=None, error_message=None)).rowcount
            if changed:
                write_result(session)
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
