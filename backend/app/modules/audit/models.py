from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_label: Mapped[str] = mapped_column(String(128))
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str] = mapped_column(String(500))
    request_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    outcome: Mapped[str] = mapped_column(String(24))
    changes: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    request_data: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    result: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
