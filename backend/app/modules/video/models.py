from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, JSON, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        Index("uq_videos_platform_identity", "platform", "platform_video_id", unique=True),
        Index("uq_videos_private_content", "owner_id", "content_sha256", unique=True),
        CheckConstraint("(source_kind = 'link' AND platform IS NOT NULL AND platform_video_id IS NOT NULL AND source_url IS NOT NULL AND owner_id IS NULL) OR (source_kind = 'upload' AND owner_id IS NOT NULL AND content_sha256 IS NOT NULL AND source_url IS NULL)", name="ck_videos_source"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    platform: Mapped[str | None] = mapped_column(String(64))
    platform_video_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(16), default="link", server_default="link")
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(16), default="video", server_default="video")
    title: Mapped[str] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    thumbnail: Mapped[str | None] = mapped_column(Text)
    raw_metadata: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
