"""Auth dependencies. Everything behind login uses require_user."""

import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_session
from backend.app.core.errors import AppError
from backend.app.core.security import decode_access_token
from backend.app.db.models import User

_bearer = HTTPBearer(auto_error=False)


async def _load_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AppError("unauthorized", "登录已失效")
    return user


async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    if creds is None or not creds.credentials:
        raise AppError("unauthorized", "需要登录")
    return await _load_user(session, decode_access_token(creds.credentials))


async def optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User | None:
    if creds is None or not creds.credentials:
        return None
    try:
        return await _load_user(session, decode_access_token(creds.credentials))
    except AppError:
        return None
