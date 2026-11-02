from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.modules.auth.dependencies import required_user
from app.modules.clips.schemas import ExportQuoteRequest, PlanQuoteRequest
from app.modules.clips.service import clip_service

router = APIRouter(prefix="/clips", tags=["clips"])


def get_clip_service():
    return clip_service(SessionLocal)


@router.post("/plans/quotes")
async def plan_quote(body: PlanQuoteRequest, user=Depends(required_user), service=Depends(get_clip_service)):
    return success_response(await service.quote_plan(user, body))


@router.get("/plans/{plan_id}")
async def plan(plan_id: UUID, user=Depends(required_user), service=Depends(get_clip_service)):
    return success_response(await service.plan(user, plan_id))


@router.post("/exports/quotes")
async def export_quote(body: ExportQuoteRequest, user=Depends(required_user), service=Depends(get_clip_service)):
    return success_response(await service.quote_export(user, body))


@router.get("/exports/{export_id}")
async def export(export_id: UUID, user=Depends(required_user), service=Depends(get_clip_service)):
    return success_response(await service.export(user, export_id))
