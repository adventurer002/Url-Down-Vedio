"""All 12 tables per docs/database.md. Structure only, no business logic."""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import BaseModel


def str_enum(*values: str, name: str) -> Enum:
    return Enum(
        *values,
        name=name,
        native_enum=False,
        validate_strings=True,
        create_constraint=True,
    )


SubscriptionStatus = str_enum("active", "expired", "cancelled", name="subscription_status")
OrderStatus = str_enum(
    "pending", "paid", "failed", "expired", "refunded", name="order_status"
)
PaymentStatus = str_enum("pending", "success", "failed", name="payment_status")
VideoStatus = str_enum("parsing", "ready", "failed", name="video_status")
DownloadStatus = str_enum(
    "queued",
    "downloading",
    "processing",
    "uploading",
    "completed",
    "failed",
    "cancelled",
    name="download_status",
)
ProcessingStatus = str_enum(
    "queued", "running", "completed", "failed", "cancelled",
    name="processing_status",
)
ProcessingTaskType = str_enum(
    "audio_extract", "transcribe", "summarize", "mindmap",
    name="processing_task_type",
)
MediaKind = str_enum("video", "audio", "subtitle", "thumbnail", name="media_kind")
UsageAction = str_enum(
    "download", "audio_extract", "transcribe", "summarize", "mindmap", "ask",
    name="usage_action",
)


class User(BaseModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")
    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    videos: Mapped[list["Video"]] = relationship(back_populates="user")


class Plan(BaseModel):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    max_daily_downloads: Mapped[int] = mapped_column(Integer, nullable=False)
    max_video_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")
    orders: Mapped[list["Order"]] = relationship(back_populates="plan")


class Subscription(BaseModel):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_user_status", "user_id", "status"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(SubscriptionStatus, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    plan: Mapped["Plan"] = relationship(back_populates="subscriptions")


class Order(BaseModel):
    __tablename__ = "orders"

    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(OrderStatus, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="orders")
    plan: Mapped["Plan"] = relationship(back_populates="orders")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")


class Payment(BaseModel):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_trade_no", name="uq_payments_provider_trade"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_trade_no: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(PaymentStatus, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    order: Mapped["Order"] = relationship(back_populates="payments")


class Video(BaseModel):
    __tablename__ = "videos"
    __table_args__ = (Index("ix_videos_platform_video", "platform", "platform_video_id"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    uploader: Mapped[str | None] = mapped_column(String(256))
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    webpage_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    formats: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    raw_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(VideoStatus, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    user: Mapped[Optional["User"]] = relationship(back_populates="videos")
    download_tasks: Mapped[list["DownloadTask"]] = relationship(back_populates="video")


class DownloadTask(BaseModel):
    __tablename__ = "download_tasks"
    __table_args__ = (Index("ix_download_tasks_user_status", "user_id", "status"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id"), nullable=False, index=True
    )
    format_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(DownloadStatus, nullable=False, index=True)
    progress: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    celery_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    video: Mapped["Video"] = relationship(back_populates="download_tasks")
    media_files: Mapped[list["MediaFile"]] = relationship(back_populates="task")


class ProcessingTask(BaseModel):
    __tablename__ = "processing_tasks"
    __table_args__ = (
        Index("ix_processing_tasks_user_status", "user_id", "status"),
        Index("ix_processing_tasks_video_type", "video_id", "task_type"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("videos.id"), nullable=False)
    task_type: Mapped[str] = mapped_column(ProcessingTaskType, nullable=False)
    status: Mapped[str] = mapped_column(ProcessingStatus, nullable=False, index=True)
    celery_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    output_id: Mapped[uuid.UUID | None] = mapped_column()
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaFile(BaseModel):
    __tablename__ = "media_files"

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("download_tasks.id"))
    video_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("videos.id"), index=True
    )
    kind: Mapped[str] = mapped_column(MediaKind, nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    storage_backend: Mapped[str] = mapped_column(String(16), default="oss", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Optional["DownloadTask"]] = relationship(back_populates="media_files")


class Transcript(BaseModel):
    __tablename__ = "transcripts"

    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("download_tasks.id"))
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    asr_engine: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)


class Summary(BaseModel):
    __tablename__ = "summaries"

    transcript_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcripts.id"), nullable=False
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    chapters: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)


class MindMap(BaseModel):
    __tablename__ = "mind_maps"

    summary_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("summaries.id"), nullable=False
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id"), nullable=False, index=True
    )
    markdown: Mapped[str] = mapped_column(Text, nullable=False)


class UsageRecord(BaseModel):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_user_created", "user_id", "created_at"),
        CheckConstraint("llm_input_tokens >= 0", name="ck_usage_input_tokens_nonneg"),
        CheckConstraint("llm_output_tokens >= 0", name="ck_usage_output_tokens_nonneg"),
        CheckConstraint("asr_seconds >= 0", name="ck_usage_asr_nonneg"),
        CheckConstraint("storage_bytes >= 0", name="ck_usage_storage_nonneg"),
        CheckConstraint("cost_cents >= 0", name="ck_usage_cost_nonneg"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(UsageAction, nullable=False)
    ref_id: Mapped[uuid.UUID | None] = mapped_column()
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    asr_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
