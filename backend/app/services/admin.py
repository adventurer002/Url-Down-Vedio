"""Admin read models and moderation commands."""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.db.models import (
    AuditLog,
    MediaFile,
    Order,
    Subscription,
    UsageRecord,
    User,
)


async def list_users(
    session: AsyncSession, search: str | None, page: int, page_size: int
) -> tuple[list[User], int]:
    stmt = select(User).order_by(User.created_at.desc())
    if search:
        like = f"%{search}%"
        stmt = stmt.where(User.email.ilike(like))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return list(rows), total


async def list_orders(
    session: AsyncSession, status: str | None, page: int, page_size: int
) -> tuple[list[Order], int]:
    stmt = select(Order).order_by(Order.created_at.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return list(rows), total


async def usage_summary(session: AsyncSession) -> list[dict[str, str | int]]:
    rows = (
        await session.execute(
            select(
                UsageRecord.action,
                func.count().label("calls"),
                func.coalesce(func.sum(UsageRecord.llm_input_tokens), 0),
                func.coalesce(func.sum(UsageRecord.llm_output_tokens), 0),
                func.coalesce(func.sum(UsageRecord.asr_seconds), 0),
                func.coalesce(func.sum(UsageRecord.cost_cents), 0),
            )
            .group_by(UsageRecord.action)
            .order_by(UsageRecord.action)
        )
    ).all()
    return [
        {
            "action": r[0],
            "calls": int(r[1]),
            "llm_input_tokens": int(r[2]),
            "llm_output_tokens": int(r[3]),
            "asr_seconds": int(r[4]),
            "cost_cents": int(r[5]),
        }
        for r in rows
    ]


async def revoke_subscription(session: AsyncSession, subscription_id: uuid.UUID) -> Subscription:
    sub = await session.get(Subscription, subscription_id)
    if sub is None:
        raise AppError("not_found", "订阅不存在")
    if sub.status != "active":
        raise AppError("validation_error", "只有生效中的订阅可撤销")
    sub.status = "cancelled"
    await session.commit()
    await session.refresh(sub)
    return sub


async def delete_media(
    session: AsyncSession,
    media_id: uuid.UUID,
    remove_blob: Callable[[str], Awaitable[None]] | None = None,
) -> MediaFile:
    from datetime import UTC, datetime

    media = await session.get(MediaFile, media_id)
    if media is None:
        raise AppError("not_found", "文件不存在")
    if media.deleted_at is None:
        media.deleted_at = datetime.now(UTC)
        await session.commit()
    if remove_blob is not None:
        await remove_blob(media.storage_key)
    await session.refresh(media)
    return media


async def cleanup_preview(session: AsyncSession) -> dict[str, int]:
    """Dry-run: count expired blobs not yet soft-deleted."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    n = (
        await session.execute(
            select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.deleted_at.is_(None), MediaFile.expires_at <= now)
        )
    ).scalar_one()
    return {"expired_pending": int(n)}


async def write_audit(
    session: AsyncSession,
    actor: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str,
    meta: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            meta=meta,
        )
    )
    await session.commit()


async def list_audit_logs(
    session: AsyncSession, page: int, page_size: int
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return list(rows), total
