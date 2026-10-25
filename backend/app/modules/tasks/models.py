from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, Integer, JSON, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
ACTIVE_STATUSES = (TaskStatus.queued, TaskStatus.running, TaskStatus.processing)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_tasks_progress"),
        CheckConstraint("attempts >= 0", name="ck_tasks_attempts"),
        CheckConstraint("status IN ('queued', 'running', 'processing', 'completed', 'failed', 'cancelled', 'expired')", name="ck_tasks_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    video_id: Mapped[UUID | None] = mapped_column(ForeignKey("videos.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus, name="task_status", validate_strings=True), default=TaskStatus.queued, server_default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    input_payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    result_payload: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    wait_reason: Mapped[str | None] = mapped_column(String(32))
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    worker_host: Mapped[str | None] = mapped_column(String(255))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
