from contextvars import ContextVar

from fastapi import Depends, Request

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.redis_client import redis_client
from app.modules.auth.repository import UserRepository
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import decode_token

# Scoped to the request; inherited by TaskService and asyncio.to_thread without global state.
task_user_id = ContextVar("task_user_id", default=None)


def get_auth_service():
    return AuthService(UserRepository(SessionLocal), redis_client)


async def optional_user(request: Request, service=Depends(get_auth_service)):
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AppException("INVALID_TOKEN", "登录凭证格式无效", 401)
        token = token.strip()
    else:
        token = request.cookies.get("access_token")
        if not token and (request.url.path.endswith("/events") or request.url.path.endswith("/download")):
            token = request.query_params.get("token")
    return await service.user(decode_token(token)) if token else None


async def required_user(user=Depends(optional_user)):
    if user is None:
        raise AppException("AUTH_REQUIRED", "请先关注公众号并登录", 401)
    return user


async def creation_user(user=Depends(optional_user)):
    if settings.AUTH_REQUIRED and user is None:
        raise AppException("AUTH_REQUIRED", "请先关注公众号并登录", 401)
    marker = task_user_id.set(user.id if user else None)
    try:
        yield user
    finally:
        task_user_id.reset(marker)


def check_owner(owner_id, user):
    if owner_id is not None and (user is None or owner_id != user.id):
        raise AppException("FORBIDDEN", "无权访问其他用户的任务", 403)
