from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base

Json = JSON().with_variant(JSONB, "postgresql")


class ClipPlan(Base):
    __tablename__ = "clip_plans"
    __table_args__ = (UniqueConstraint("user_id", "cache_key", name="uq_clip_plan_owner_key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), unique=True)
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id"), index=True)
    transcript_id: Mapped[UUID] = mapped_column(ForeignKey("transcripts.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClipTimeline(Base):
    __tablename__ = "clip_timelines"
    __table_args__ = (UniqueConstraint("user_id", "cache_key", name="uq_clip_timeline_owner_key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("clip_plans.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClipExport(Base):
    __tablename__ = "clip_exports"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), unique=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("clip_plans.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClipExportItem(Base):
    __tablename__ = "clip_export_items"
    __table_args__ = (CheckConstraint("position >= 0", name="ck_clip_item_position"),
                      UniqueConstraint("export_id", "file_id", name="uq_clip_item_export_file"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    export_id: Mapped[UUID] = mapped_column(ForeignKey("clip_exports.id"), index=True)
    # Keep the relationship after the media row is removed by the existing TTL cleaner.
    file_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    asset_key: Mapped[str | None] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(Json)
