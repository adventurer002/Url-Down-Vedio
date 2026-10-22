from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, Index, JSON, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class TranscriptSource(str, Enum):
    subtitle = "subtitle"
    asr = "asr"


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        Index("uq_transcripts_cache", "video_id", "source_type", "language", "content_version", "asr_model", unique=True),
        CheckConstraint("source_type IN ('subtitle', 'asr')", name="ck_transcripts_source"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"))
    source_type: Mapped[TranscriptSource] = mapped_column(SAEnum(TranscriptSource, name="transcript_source", validate_strings=True))
    language: Mapped[str] = mapped_column(String(32))
    content_version: Mapped[str] = mapped_column(String(64), default="v1", server_default="v1")
    asr_model: Mapped[str] = mapped_column(String(128), default="none", server_default="none")
    content_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    segments: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
