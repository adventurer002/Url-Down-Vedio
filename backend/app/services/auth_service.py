"""User registration / login / refresh. Route layer only validates + delegates."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.core.security import (
    create_access_token,
    hash_password,
    mint_refresh_token,
    rotate_refresh_token,
    store_refresh_token,
    verify_password,
)
from backend.app.db.models import User
from backend.app.schemas.auth import UserOut


async def register_user(
    session: AsyncSession, email: str, password: str, nickname: str | None
) -> tuple[User, str, str]:
    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError("validation_error", "邮箱已被注册")
    user = User(
        email=email,
        password_hash=hash_password(password),
        nickname=nickname or email.split("@")[0],
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    refresh, refresh_hash = mint_refresh_token()
    await store_refresh_token(refresh_hash, user.id)
    return user, create_access_token(user.id, user.is_admin), refresh


async def login_user(session: AsyncSession, email: str, password: str) -> tuple[User, str, str]:
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        raise AppError("unauthorized", "邮箱或密码错误")
    if not user.is_active:
        raise AppError("unauthorized", "账号已被禁用")
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    refresh, refresh_hash = mint_refresh_token()
    await store_refresh_token(refresh_hash, user.id)
    return user, create_access_token(user.id, user.is_admin), refresh


async def refresh_pair(session: AsyncSession, refresh_token: str) -> tuple[User, str, str]:
    rotated = await rotate_refresh_token(refresh_token)
    if rotated is None:
        raise AppError("unauthorized", "登录已失效")
    user_id, new_refresh = rotated
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AppError("unauthorized", "登录已失效")
    return user, create_access_token(user.id, user.is_admin), new_refresh


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        nickname=user.nickname,
        is_admin=user.is_admin,
    )
