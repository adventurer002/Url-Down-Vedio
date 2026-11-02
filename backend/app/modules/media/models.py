from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (CheckConstraint("size_bytes >= 0", name="ck_media_files_size"),
                     CheckConstraint("purpose IN ('legacy','full_source','full_audio','clip')", name="ck_media_purpose"))

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id"), index=True)
    preset_id: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(String(16), default="legacy", server_default="legacy")
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_start: Mapped[float | None] = mapped_column(Float)
    source_end: Mapped[float | None] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
