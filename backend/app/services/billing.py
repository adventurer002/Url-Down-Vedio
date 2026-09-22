"""Plans, orders, subscriptions. Amounts always snapshotted server-side."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.db.models import Order, Plan, Subscription, User

ORDER_TTL = timedelta(minutes=30)


def new_order_no() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S") + secrets.token_hex(6).upper()


async def get_plan_by_code(session: AsyncSession, code: str) -> Plan:
    plan = (
        await session.execute(
            select(Plan).where(Plan.code == code, Plan.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if plan is None:
        raise AppError("not_found", "套餐不存在")
    return plan


async def create_order(session: AsyncSession, user_id: uuid.UUID, plan_code: str) -> Order:
    plan = await get_plan_by_code(session, plan_code)
    now = datetime.now(UTC)
    order = Order(
        order_no=new_order_no(),
        user_id=user_id,
        plan_id=plan.id,
        amount_cents=plan.price_cents,
        currency=plan.currency,
        status="pending",
        expired_at=now + ORDER_TTL,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def activate_subscription(
    session: AsyncSession,
    user_id: uuid.UUID,
    plan: Plan,
    months: int = 1,
) -> Subscription:
    """Manual/admin grant. Supersedes any active sub; single transaction."""
    now = datetime.now(UTC)
    existing = (
        await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status == "active",
            )
        )
    ).scalars().all()
    for sub in existing:
        sub.status = "cancelled"
    duration = timedelta(days=(plan.duration_days or 30) * months)
    sub = Subscription(
        user_id=user_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        expires_at=now + duration,
        auto_renew=False,
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    return sub


async def grant_for_order(
    session: AsyncSession, order_id: uuid.UUID
) -> tuple[Order, Subscription]:
    """Mark a pending order paid and open the subscription, atomically."""
    order = await session.get(Order, order_id)
    if order is None:
        raise AppError("not_found", "订单不存在")
    if order.status != "pending":
        raise AppError("validation_error", "订单状态不可开通")
    plan = await session.get(Plan, order.plan_id)
    if plan is None:
        raise AppError("not_found", "套餐不存在")
    order.status = "paid"
    order.paid_at = datetime.now(UTC)
    await session.flush()
    sub = await activate_subscription(session, order.user_id, plan)
    await session.refresh(order)
    return order, sub


async def expire_due(session: AsyncSession) -> dict[str, int]:
    """Beat task body: expire subscriptions and stale pending orders."""
    now = datetime.now(UTC)
    subs = (
        await session.execute(
            select(Subscription).where(
                Subscription.status == "active",
                Subscription.expires_at <= now,
            )
        )
    ).scalars().all()
    for sub in subs:
        sub.status = "expired"
    orders = (
        await session.execute(
            select(Order).where(
                Order.status == "pending",
                Order.expired_at.is_not(None),
                Order.expired_at <= now,
            )
        )
    ).scalars().all()
    for order in orders:
        order.status = "expired"
    await session.commit()
    return {"subscriptions": len(subs), "orders": len(orders)}


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
