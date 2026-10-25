from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, JSON, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base

Json = JSON().with_variant(JSONB, "postgresql")


class AllowanceAccount(Base):
    __tablename__ = "allowance_accounts"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(64), unique=True)
    trial_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    member_anchor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    member_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    member_months: Mapped[int] = mapped_column(Integer, default=0)


class Allowance(Base):
    __tablename__ = "allowances"
    __table_args__ = (CheckConstraint("granted >= 0 AND used >= 0 AND held >= 0 AND used + held <= granted", name="ck_allowance_amounts"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), index=True)
    source_key: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(24))
    granted: Mapped[int] = mapped_column(Integer)
    used: Mapped[int] = mapped_column(Integer, default=0)
    held: Mapped[int] = mapped_column(Integer, default=0)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AllowanceGrant(Base):
    __tablename__ = "allowance_grants"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), index=True)
    product: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(128))
    snapshot: Mapped[dict] = mapped_column(Json)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AllowanceQuote(Base):
    __tablename__ = "allowance_quotes"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), index=True)
    task_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(Json)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AllowanceReservation(Base):
    __tablename__ = "allowance_reservations"
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), index=True)
    state: Mapped[str] = mapped_column(String(16), default="held")
    slot: Mapped[str] = mapped_column(String(16))
    business_date: Mapped[date] = mapped_column(Date)
    free_download: Mapped[int] = mapped_column(Integer, default=0)
    asr_seconds: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[dict] = mapped_column(Json)
    settlement: Mapped[dict | None] = mapped_column(Json)


class AllowanceAllocation(Base):
    __tablename__ = "allowance_allocations"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_allowance_allocation"),)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_reservations.task_id"), primary_key=True)
    allowance_id: Mapped[UUID] = mapped_column(ForeignKey("allowances.id"), primary_key=True)
    amount: Mapped[int] = mapped_column(Integer)


class AllowanceEvent(Base):
    __tablename__ = "allowance_events"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), index=True)
    event: Mapped[str] = mapped_column(String(24))
    detail: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyAllowance(Base):
    __tablename__ = "daily_allowances"
    __table_args__ = (CheckConstraint("downloads_used >= 0 AND downloads_held >= 0 AND egress_used >= 0 AND egress_held >= 0", name="ck_daily_allowances"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    downloads_used: Mapped[int] = mapped_column(Integer, default=0)
    downloads_held: Mapped[int] = mapped_column(Integer, default=0)
    egress_used: Mapped[int] = mapped_column(BigInteger, default=0)
    egress_held: Mapped[int] = mapped_column(BigInteger, default=0)


class AllowanceTransfer(Base):
    __tablename__ = "allowance_transfers"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("allowance_accounts.user_id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    reserved: Mapped[int] = mapped_column(BigInteger)
    sent: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
