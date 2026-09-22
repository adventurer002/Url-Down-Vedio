"""PermissionService: the ONLY place membership checks live (api.md §3 matrix)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Plan, Subscription, UsageRecord


@dataclass
class PermissionResult:
    allowed: bool
    code: str = ""
    message: str = ""


async def current_plan(session: AsyncSession, user_id: uuid.UUID) -> Plan:
    """Latest active, unexpired subscription; falls back to the free plan."""
    now = datetime.now(UTC)
    sub = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == "active",
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sub is not None:
        plan = await session.get(Plan, sub.plan_id)
        if plan is not None and plan.is_active:
            return plan
    free = (
        await session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    return free


async def today_downloads_used(session: AsyncSession, user_id: uuid.UUID) -> int:
    n = (
        await session.execute(
            select(func.count())
            .select_from(UsageRecord)
            .where(
                UsageRecord.user_id == user_id,
                UsageRecord.action == "download",
                func.date(UsageRecord.created_at) == func.current_date(),
            )
        )
    ).scalar_one()
    return int(n)


async def record_download_usage(
    session: AsyncSession, user_id: uuid.UUID, ref_id: uuid.UUID | None
) -> None:
    session.add(UsageRecord(user_id=user_id, action="download", ref_id=ref_id))
    await session.commit()


async def month_usage_totals(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    month = datetime.now(UTC).strftime("%Y-%m")
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageRecord.llm_input_tokens), 0),
                func.coalesce(func.sum(UsageRecord.llm_output_tokens), 0),
                func.coalesce(func.sum(UsageRecord.asr_seconds), 0),
                func.coalesce(func.sum(UsageRecord.cost_cents), 0),
            ).where(
                UsageRecord.user_id == user_id,
                func.to_char(UsageRecord.created_at, "YYYY-MM") == month,
            )
        )
    ).one()
    return {
        "month_llm_input_tokens": int(row[0]),
        "month_llm_output_tokens": int(row[1]),
        "month_asr_seconds": int(row[2]),
        "month_cost_cents": int(row[3]),
    }


async def can_download(
    session: AsyncSession, user_id: uuid.UUID, duration_seconds: int | None
) -> PermissionResult:
    plan = await current_plan(session, user_id)
    features = plan.features or {}
    if not features.get("download", True):
        return PermissionResult(False, "permission_denied", "当前套餐不支持下载")
    used = await today_downloads_used(session, user_id)
    if used >= plan.max_daily_downloads:
        return PermissionResult(False, "quota_exceeded", "今日免费下载次数已用完")
    if duration_seconds is not None and duration_seconds > plan.max_video_duration_seconds:
        return PermissionResult(
            False, "quota_exceeded", "视频时长超出当前套餐上限",
            )
    return PermissionResult(True)
