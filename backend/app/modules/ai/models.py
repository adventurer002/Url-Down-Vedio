from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, JSON, Numeric, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class AIResultType(str, Enum):
    summary = "summary"
    mindmap = "mindmap"


class AIResult(Base):
    __tablename__ = "ai_results"
    __table_args__ = (
        Index("uq_ai_results_cache", "source_content_hash", "result_type", "llm_model", "prompt_version", "output_language", unique=True),
        CheckConstraint("result_type IN ('summary', 'mindmap')", name="ck_ai_results_type"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_ai_input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_ai_output_tokens"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"))
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("transcripts.id"), index=True)
    result_type: Mapped[AIResultType] = mapped_column(SAEnum(AIResultType, name="ai_result_type", validate_strings=True))
    source_content_hash: Mapped[str] = mapped_column(String(64))
    llm_provider: Mapped[str] = mapped_column(String(64))
    llm_model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    output_language: Mapped[str] = mapped_column(String(32))
    content_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    markdown_export: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
