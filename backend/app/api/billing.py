"""Plans, orders, webhooks, reconcile (Phase8: Stripe + WeChat + Alipay)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth_deps import require_user
from backend.app.api.deps import get_session
from backend.app.core.errors import AppError
from backend.app.db.models import Order, Plan, User
from backend.app.providers.payment import PaymentError
from backend.app.schemas.billing import (
    GrantRequest,
    GrantResponse,
    OrderCreate,
    OrderCreateResponse,
    OrderOut,
    PayParamsOut,
    PlanOut,
)
from backend.app.schemas.media import Page
from backend.app.services import billing as billing_service

router = APIRouter(prefix="/api/v1")


def _plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        code=plan.code,
        name=plan.name,
        price_cents=plan.price_cents,
        currency=plan.currency,
        duration_days=plan.duration_days,
        features=plan.features or {},
        max_daily_downloads=plan.max_daily_downloads,
        max_video_duration_seconds=plan.max_video_duration_seconds,
        sort_order=plan.sort_order,
    )


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(session: AsyncSession = Depends(get_session)) -> list[PlanOut]:
    plans = (
        await session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order)
        )
    ).scalars().all()
    return [_plan_out(p) for p in plans]


async def _order_out(session: AsyncSession, order: Order) -> OrderOut:
    plan = await session.get(Plan, order.plan_id)
    return OrderOut(
        order_no=order.order_no,
        plan_code=plan.code if plan else "",
        amount_cents=order.amount_cents,
        currency=order.currency,
        status=order.status,
        paid_at=order.paid_at,
        expired_at=order.expired_at,
    )


@router.post("/orders", response_model=OrderCreateResponse, status_code=201)
async def create_order(
    body: OrderCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> OrderCreateResponse:
    order = await billing_service.create_order(session, user.id, body.plan_code)
    params = await billing_service.build_pay_params(session, order, body.provider)
    return OrderCreateResponse(
        order_no=order.order_no,
        amount_cents=order.amount_cents,
        currency=order.currency,
        pay_params=PayParamsOut(
            provider=params.provider,
            pay_url=params.pay_url,
            qr_code=params.qr_code,
            client_secret=params.client_secret,
        ),
        providers=billing_service.enabled_providers(),
    )


@router.get("/orders", response_model=Page)
async def list_orders(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> Page:
    orders = (
        await session.execute(
            select(Order)
            .where(Order.user_id == user.id)
            .order_by(Order.created_at.desc())
        )
    ).scalars().all()
    return Page(
        items=[await _order_out(session, o) for o in orders],
        total=len(orders),
        page=1,
        page_size=len(orders),
    )


@router.get("/orders/{order_no}", response_model=OrderOut)
async def get_order(
    order_no: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> OrderOut:
    order = (
        await session.execute(
            select(Order).where(Order.order_no == order_no, Order.user_id == user.id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise AppError("not_found", "订单不存在")
    return await _order_out(session, order)


@router.post("/webhooks/payments/{provider}")
async def payment_webhook(provider: str, request: Request) -> dict[str, str]:
    from backend.app.db.session import SessionLocal

    channel = billing_service.get_provider(provider)
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = channel.verify_webhook(headers, body)
    except PaymentError:
        raise AppError("validation_error", "签名校验失败")
    async with SessionLocal() as session:
        result, _ = await billing_service.apply_webhook_event(session, event)
    return {"result": result}


@router.post("/admin/orders/{order_no}/reconcile", response_model=OrderOut)
async def reconcile_order(
    order_no: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> OrderOut:
    if not user.is_admin:
        raise AppError("permission_denied", "需要管理员权限")
    order = (
        await session.execute(select(Order).where(Order.order_no == order_no))
    ).scalar_one_or_none()
    if order is None:
        raise AppError("not_found", "订单不存在")
    _, updated = await billing_service.reconcile_order(session, order)
    await session.refresh(updated)
    return await _order_out(session, updated)


@router.post("/admin/grant", response_model=GrantResponse)
async def grant_subscription(
    body: GrantRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> GrantResponse:
    if not user.is_admin:
        raise AppError("permission_denied", "需要管理员权限")
    target = await billing_service.find_user_by_email(session, body.email)
    if target is None:
        raise AppError("not_found", "用户不存在")
    plan = await billing_service.get_plan_by_code(session, body.plan_code)
    sub = await billing_service.activate_subscription(session, target.id, plan)
    return GrantResponse(
        user_id=target.id,
        plan_code=plan.code,
        subscription_id=sub.id,
        expires_at=sub.expires_at,
    )
