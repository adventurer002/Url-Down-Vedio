"""Plans, orders, subscriptions. Amounts always snapshotted server-side."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.errors import AppError
from backend.app.db.models import Order, Payment, Plan, Subscription, User
from backend.app.providers.alipay_pay import AlipayProvider
from backend.app.providers.payment import PaymentError, PayParams, WebhookEvent
from backend.app.providers.stripe_pay import StripeProvider
from backend.app.providers.wechat_pay import WechatPayProvider

ORDER_TTL = timedelta(minutes=30)


def _pem(value: str) -> str:
    """Env files cannot hold literal newlines; accept escaped ones."""
    return value.replace("\\n", "\n")


def get_provider(name: str) -> StripeProvider | WechatPayProvider | AlipayProvider:
    if name == "stripe":
        if not settings.stripe_enabled:
            raise AppError("not_found", "该支付渠道未开通")
        try:
            return StripeProvider(
                settings.stripe_secret_key,
                settings.stripe_webhook_secret,
                settings.stripe_success_url,
                settings.stripe_cancel_url,
            )
        except PaymentError as e:
            raise AppError("internal_error", str(e))
    if name == "wechat":
        if not settings.wechat_enabled:
            raise AppError("not_found", "该支付渠道未开通")
        try:
            return WechatPayProvider(
                settings.wechat_mchid,
                settings.wechat_appid,
                settings.wechat_serial_no,
                _pem(settings.wechat_private_key),
                settings.wechat_apiv3_key,
                settings.wechat_notify_url,
            )
        except PaymentError as e:
            raise AppError("internal_error", str(e))
    if name == "alipay":
        if not settings.alipay_enabled:
            raise AppError("not_found", "该支付渠道未开通")
        try:
            return AlipayProvider(
                settings.alipay_app_id,
                _pem(settings.alipay_private_key),
                _pem(settings.alipay_public_key),
                settings.alipay_notify_url,
                settings.alipay_return_url,
            )
        except PaymentError as e:
            raise AppError("internal_error", str(e))
    raise AppError("not_found", "未知的支付渠道")


def enabled_providers() -> list[str]:
    out = []
    if settings.stripe_enabled:
        out.append("stripe")
    if settings.wechat_enabled:
        out.append("wechat")
    if settings.alipay_enabled:
        out.append("alipay")
    return out


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


async def build_pay_params(
    session: AsyncSession, order: Order, provider_name: str
) -> PayParams:
    """Create channel payment from the SNAPSHOT amount. Never trusts input."""
    if order.status != "pending":
        raise AppError("validation_error", "订单状态不可支付")
    plan = await session.get(Plan, order.plan_id)
    if plan is None:
        raise AppError("not_found", "套餐不存在")
    provider = get_provider(provider_name)
    try:
        return await provider.create_payment(
            order.order_no, order.amount_cents, order.currency, plan.name
        )
    except PaymentError as e:
        raise AppError("download_failed", str(e))


async def apply_webhook_event(
    session: AsyncSession, event: WebhookEvent
) -> tuple[str, Order | None]:
    """Idempotent: (provider, provider_trade_no) is unique; repeats are no-ops.

    Returns ("granted" | "duplicate" | "ignored", order).
    """
    payment = (
        await session.execute(
            select(Payment).where(
                Payment.provider == event.provider,
                Payment.provider_trade_no == event.provider_trade_no,
            )
        )
    ).scalar_one_or_none()
    if payment is not None:
        return "duplicate", await session.get(Order, payment.order_id)
    order = (
        await session.execute(
            select(Order).where(Order.order_no == event.order_no)
        )
    ).scalar_one_or_none()
    if order is None:
        return "ignored", None
    now = datetime.now(UTC)
    if event.status != "success":
        payment = Payment(
            order_id=order.id,
            provider=event.provider,
            provider_trade_no=event.provider_trade_no,
            amount_cents=event.amount_cents,
            currency=order.currency,
            status="failed",
            raw_payload=event.raw,
        )
        session.add(payment)
        await session.commit()
        return "ignored", order
    if event.amount_cents != order.amount_cents:
        payment = Payment(
            order_id=order.id,
            provider=event.provider,
            provider_trade_no=event.provider_trade_no,
            amount_cents=event.amount_cents,
            currency=order.currency,
            status="failed",
            raw_payload={"reason": "amount_mismatch", **event.raw},
        )
        session.add(payment)
        await session.commit()
        return "ignored", order
    payment = Payment(
        order_id=order.id,
        provider=event.provider,
        provider_trade_no=event.provider_trade_no,
        amount_cents=event.amount_cents,
        currency=order.currency,
        status="success",
        paid_at=now,
        raw_payload=event.raw,
    )
    session.add(payment)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return "duplicate", order
    if order.status == "paid":
        await session.commit()
        return "duplicate", order
    if order.status != "pending":
        await session.commit()
        return "ignored", order
    await grant_for_order(session, order.id)
    await session.commit()
    return "granted", order


async def reconcile_order(
    session: AsyncSession, order: Order
) -> tuple[str, Order]:
    """Manual pickup: query every enabled channel for this order's status."""
    if order.status == "paid":
        return "duplicate", order
    for name in enabled_providers():
        provider = get_provider(name)
        try:
            event = await provider.query_status(order.order_no)
        except PaymentError:
            continue
        if event is None or event.status != "success":
            continue
        result, _ = await apply_webhook_event(session, event)
        await session.refresh(order)
        return result, order
    return "ignored", order
