"""Audit trail for admin writes. Reads actor from the verified JWT."""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from backend.app.core.config import settings
from backend.app.core.security import decode_access_token

AUDIT_PREFIXES = ("/api/v1/admin/",)

_engine = None


def _maker() -> async_sessionmaker[AsyncSession]:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url)
    return async_sessionmaker(_engine, expire_on_commit=False)


def _action_for(method: str, path: str) -> str | None:
    if method == "POST" and path.endswith("/grant"):
        return "grant"
    if method == "POST" and path.endswith("/reconcile"):
        return "reconcile"
    if method == "POST" and "/revoke" in path:
        return "revoke"
    if method == "DELETE" and "/media/" in path:
        return "delete_media"
    if method == "POST" and path.endswith("/cleanup"):
        return "cleanup"
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if request.method not in ("POST", "DELETE"):
            return response
        if not any(path.startswith(p) for p in AUDIT_PREFIXES):
            return response
        if response.status_code >= 400:
            return response
        action = _action_for(request.method, path)
        if action is None:
            return response
        actor: uuid.UUID | None = None
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                actor = decode_access_token(auth[7:])
            except Exception:  # noqa: BLE001 - audit must never break the request
                actor = None
        try:
            override = getattr(request.app.state, "audit_session_maker", None)
            maker = override if override is not None else _maker()
            async with maker() as session:
                from backend.app.services import admin as admin_service

                await admin_service.write_audit(
                    session,
                    actor,
                    action,
                    "admin",
                    path,
                    {"status": response.status_code},
                )
        except Exception as exc:  # noqa: BLE001 - audit must never break the request
            structlog.get_logger().warning("audit_write_failed", error=str(exc))
        return response
