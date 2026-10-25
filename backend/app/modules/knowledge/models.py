from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base

Json = JSON().with_variant(JSONB, "postgresql")


class VideoAnswer(Base):
    __tablename__ = "video_answers"
    __table_args__ = (UniqueConstraint("user_id", "question_key", name="uq_video_answer_owner_key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), unique=True)
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("transcripts.id"), index=True)
    question_key: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(String(1000))
    answer: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(Json)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    output_language: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SavedVideo(Base):
    __tablename__ = "saved_videos"
    __table_args__ = (UniqueConstraint("user_id", "transcript_id", name="uq_saved_video_owner_transcript"),
                      Index("ix_saved_videos_owner_updated", "user_id", "updated_at"))
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("transcripts.id"))
    result_id: Mapped[UUID | None] = mapped_column(ForeignKey("ai_results.id"))
    title: Mapped[str] = mapped_column(String(1000))
    video: Mapped[dict] = mapped_column(Json)
    source_type: Mapped[str] = mapped_column(String(16))
    content_text: Mapped[str] = mapped_column(Text)
    summary_text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(Json)
    tags_text: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
