"""Admin console API: users, orders, usage, moderation. Admin only."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth_deps import require_user
from backend.app.api.deps import get_session
from backend.app.core.errors import AppError
from backend.app.db.models import User
from backend.app.schemas.admin import (
    AdminOrderOut,
    AdminUserOut,
    AuditLogOut,
    CleanupPreview,
    PageOut,
    UsageSummaryOut,
)
from backend.app.services import admin as admin_service

router = APIRouter(prefix="/api/v1/admin")


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise AppError("permission_denied", "需要管理员权限")


@router.get("/users", response_model=PageOut)
async def admin_users(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PageOut:
    _require_admin(user)
    rows, total = await admin_service.list_users(session, search, page, page_size)
    return PageOut(
        items=[
            AdminUserOut(
                id=r.id,
                email=r.email,
                nickname=r.nickname,
                is_admin=r.is_admin,
                is_active=r.is_active,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/orders", response_model=PageOut)
async def admin_orders(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PageOut:
    _require_admin(user)
    rows, total = await admin_service.list_orders(session, status, page, page_size)
    return PageOut(
        items=[
            AdminOrderOut(
                order_no=r.order_no,
                user_id=r.user_id,
                amount_cents=r.amount_cents,
                currency=r.currency,
                status=r.status,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/usage/summary", response_model=list[UsageSummaryOut])
async def admin_usage(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> list[UsageSummaryOut]:
    _require_admin(user)
    rows = await admin_service.usage_summary(session)
    return [
        UsageSummaryOut(
            action=str(r["action"]),
            calls=int(r["calls"]),
            llm_input_tokens=int(r["llm_input_tokens"]),
            llm_output_tokens=int(r["llm_output_tokens"]),
            asr_seconds=int(r["asr_seconds"]),
            cost_cents=int(r["cost_cents"]),
        )
        for r in rows
    ]


@router.post("/subscriptions/{subscription_id}/revoke")
async def admin_revoke(
    subscription_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> dict[str, str]:
    _require_admin(user)
    sub = await admin_service.revoke_subscription(session, subscription_id)
    return {"subscription_id": str(sub.id), "status": sub.status}


@router.delete("/media/{media_id}")
async def admin_delete_media(
    media_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> dict[str, str]:
    _require_admin(user)
    from backend.app.worker.tasks import build_storage

    media = await admin_service.delete_media(
        session, media_id, build_storage().delete
    )
    return {"media_id": str(media.id), "deleted": "true"}


@router.post("/media/cleanup", response_model=CleanupPreview)
async def admin_cleanup_preview(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> CleanupPreview:
    _require_admin(user)
    preview = await admin_service.cleanup_preview(session)
    return CleanupPreview(**preview)


@router.get("/audit-logs", response_model=PageOut)
async def admin_audit_logs(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PageOut:
    _require_admin(user)
    rows, total = await admin_service.list_audit_logs(session, page, page_size)
    return PageOut(
        items=[
            AuditLogOut(
                id=r.id,
                actor_user_id=r.actor_user_id,
                action=r.action,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
