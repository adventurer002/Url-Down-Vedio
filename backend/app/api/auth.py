"""Auth + me + usage routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth_deps import optional_user, require_user
from backend.app.api.deps import get_session
from backend.app.db.models import User
from backend.app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UsageResponse,
)
from backend.app.services import auth_service as auth
from backend.app.services import permission as perm

router = APIRouter(prefix="/api/v1")


@router.post("/auth/register", response_model=AuthResponse, status_code=201)
async def register(
    body: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> AuthResponse:
    user, access, refresh = await auth.register_user(
        session, str(body.email), body.password, body.nickname
    )
    return AuthResponse(
        access_token=access,
        refresh_token=refresh,
        user=auth.user_out(user),
    )


@router.post("/auth/login", response_model=AuthResponse)
async def login(
    body: LoginRequest, session: AsyncSession = Depends(get_session)
) -> AuthResponse:
    user, access, refresh = await auth.login_user(session, str(body.email), body.password)
    return AuthResponse(
        access_token=access,
        refresh_token=refresh,
        user=auth.user_out(user),
    )


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    _, access, new_refresh = await auth.refresh_pair(session, body.refresh_token)
    return TokenPair(access_token=access, refresh_token=new_refresh)


@router.get("/users/me", response_model=MeResponse)
async def me(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> MeResponse:
    plan = await perm.current_plan(session, user.id)
    used = await perm.today_downloads_used(session, user.id)
    return MeResponse(
        user=auth.user_out(user),
        plan_code=plan.code,
        plan_name=plan.name,
        today_downloads_used=used,
        today_downloads_quota=plan.max_daily_downloads,
    )


@router.get("/usage", response_model=UsageResponse)
async def usage(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> UsageResponse:
    plan = await perm.current_plan(session, user.id)
    used = await perm.today_downloads_used(session, user.id)
    month = await perm.month_usage_totals(session, user.id)
    return UsageResponse(
        plan_code=plan.code,
        today_downloads_used=used,
        today_downloads_quota=plan.max_daily_downloads,
        max_video_duration_seconds=plan.max_video_duration_seconds,
        month_llm_input_tokens=month["month_llm_input_tokens"],
        month_llm_output_tokens=month["month_llm_output_tokens"],
        month_asr_seconds=month["month_asr_seconds"],
        month_cost_cents=month["month_cost_cents"],
    )


@router.get("/auth/ping")
async def ping(user: User | None = Depends(optional_user)) -> dict[str, object]:
    return {"user_id": str(user.id) if user else None}
