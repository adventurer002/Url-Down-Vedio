from contextvars import ContextVar
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPBearer

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.redis_client import redis_client
from app.modules.auth.repository import UserRepository
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import decode_identity, decode_resource_identity

# Scoped to the request; inherited by TaskService and asyncio.to_thread without global state.
task_user_id = ContextVar("task_user_id", default=None)
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


def get_auth_service():
    return AuthService(UserRepository(SessionLocal), redis_client)


async def optional_user(request: Request, service=Depends(get_auth_service), credentials=Depends(bearer_scheme)):
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AppException("INVALID_TOKEN", "登录凭证格式无效", 401)
        token = token.strip()
    else:
        token = request.cookies.get("access_token")
    return await service.user(*decode_identity(token)) if token else None


async def resource_user(request: Request, service=Depends(get_auth_service), credentials=Depends(bearer_scheme)):
    # Only download/events routes use this dependency; tickets never authenticate normal API calls.
    ticket = request.query_params.get("access_token")
    if ticket:
        try:
            if "task_id" in request.path_params:
                purpose, resource_id = "task_events", UUID(str(request.path_params["task_id"]))
            else:
                purpose, resource_id = "file_download", UUID(str(request.path_params["file_id"]))
        except (ValueError, TypeError, KeyError):
            raise AppException("INVALID_RESOURCE_ACCESS", "资源访问链接无效", 401) from None
        return await service.user(*decode_resource_identity(ticket, purpose, resource_id))
    return await optional_user(request, service, credentials)


async def required_user(user=Depends(optional_user)):
    if user is None:
        raise AppException("AUTH_REQUIRED", "请先关注公众号并登录", 401)
    return user


async def creation_user(user=Depends(optional_user)):
    if (settings.AUTH_REQUIRED or settings.ALLOWANCES_ENABLED) and user is None:
        raise AppException("AUTH_REQUIRED", "请先关注公众号并登录", 401)
    marker = task_user_id.set(user.id if user else None)
    try:
        yield user
    finally:
        task_user_id.reset(marker)


def check_owner(owner_id, user):
    if settings.ALLOWANCES_ENABLED and owner_id is None:
        raise AppException("FORBIDDEN", "旧匿名任务需要管理员确认归属后才能访问", 403)
    if owner_id is not None and (user is None or owner_id != user.id):
        raise AppException("FORBIDDEN", "无权访问其他用户的任务", 403)


async def required_admin(user=Depends(required_user)):
    if user.role != "admin":
        raise AppException("FORBIDDEN", "此操作仅限管理员", 403)
    return user


async def admin_writer(request: Request, user=Depends(required_admin)):
    # Existing clients already use Bearer headers. Refuse cookie-only mutations
    # rather than depending on browser SameSite behavior for administrative writes.
    if not request.headers.get("Authorization", "").lower().startswith("bearer "):
        raise AppException("ADMIN_BEARER_REQUIRED", "管理写操作需要 Header Bearer 登录凭证", 403)
    return user
