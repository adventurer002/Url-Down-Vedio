from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (
        CheckConstraint("expected_bytes > 0 AND size_bytes >= 0", name="ck_uploads_size"),
        CheckConstraint("status IN ('pending','uploaded','validating','ready','failed','expired','deleted')", name="ck_uploads_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    extension: Mapped[str] = mapped_column(String(8))
    expected_bytes: Mapped[int] = mapped_column(BigInteger)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", index=True)
    video_id: Mapped[UUID | None] = mapped_column(ForeignKey("videos.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), unique=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    media_type: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
