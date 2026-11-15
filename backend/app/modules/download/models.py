"""A prepared, account-scoped download. Upstream credentials stay server-side."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class DownloadDelivery(Base):
    __tablename__ = "download_deliveries"

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    active_token: Mapped[UUID | None] = mapped_column(Uuid)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fallback_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"))
    fallback_quote_id: Mapped[UUID | None] = mapped_column(ForeignKey("allowance_quotes.id"))
    charged: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_error: Mapped[str | None] = mapped_column(String(64))
