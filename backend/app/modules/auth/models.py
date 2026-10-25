from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    wechat_openid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    wechat_unionid: Mapped[str | None] = mapped_column(String(64), unique=True)
    nickname: Mapped[str] = mapped_column(String(64))
    is_subscribed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    role: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
