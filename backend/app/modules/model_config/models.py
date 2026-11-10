"""Immutable model revisions; credentials never enter task or audit payloads."""

from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.infrastructure.database.base import Base
from app.modules.billing.models import Json


class ModelResource(Base):
    __tablename__ = "model_resources"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelRevision(Base):
    __tablename__ = "model_revisions"
    __table_args__ = (
        UniqueConstraint("resource_id", "version", name="uq_model_revision_version"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    resource_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_resources.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelCredential(Base):
    __tablename__ = "model_credentials"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    ciphertext: Mapped[str] = mapped_column(Text)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelRelease(Base):
    __tablename__ = "model_releases"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    bindings: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelConfigState(Base):
    __tablename__ = "model_config_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    release_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_releases.id"))


class ModelProbe(Base):
    __tablename__ = "model_probes"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_revisions.id"), index=True
    )
    phase: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_revisions.id"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(24))
    success: Mapped[bool] = mapped_column(Boolean)
    duration_ms: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
