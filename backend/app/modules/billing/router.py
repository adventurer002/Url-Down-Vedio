from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.auth.dependencies import required_user
from app.modules.billing.policy import catalog
from app.modules.billing.repository import AllowanceRepository
from app.modules.billing.service import AllowanceDispatcher, BillingService, resume_queued
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.video.parsers.cached import get_video_parser
from app.modules.video.repository import VideoRepository
from app.modules.video.resolver import VideoResolver

router = APIRouter(prefix="/billing", tags=["billing"])


def get_billing_service():
    if not settings.ALLOWANCES_ENABLED:
        raise AppException("ALLOWANCES_DISABLED", "额度功能未启用", 503)
    return BillingService(AllowanceRepository(SessionLocal), VideoResolver(get_video_parser(), VideoRepository(SessionLocal)),
        VideoRepository(SessionLocal), TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client), AllowanceDispatcher()))


from app.modules.video.sources import AIInputRequest


class QuoteRequest(AIInputRequest):
    model_config = ConfigDict(extra="forbid")
    action: Literal["download", "audio_extraction", "transcription", "ai_summary", "ai_mindmap"]
    preset_id: Literal["video_1080_mp4", "video_720_mp4", "video_480_mp4", "audio_mp3"] = "video_720_mp4"
    language: str = Field(default="zh", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=32)


    @model_validator(mode="after")
    def transcript_action(self):
        if self.transcript_id is not None and self.action not in {"ai_summary", "ai_mindmap"}:
            raise ValueError("transcript_id 仅支持总结或思维导图")
        return self


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: UUID


@router.get("/catalog")
async def product_catalog():
    return success_response(catalog())


@router.get("/allowances")
async def allowances(user=Depends(required_user), service=Depends(get_billing_service)):
    return success_response(await database_call(service.repository.balance, user.id))


@router.get("/ledger")
async def ledger(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                 user=Depends(required_user), service=Depends(get_billing_service)):
    return success_response({"items": await database_call(service.repository.ledger, user.id, limit, offset)})


@router.post("/quotes")
async def quote(body: QuoteRequest, user=Depends(required_user), service=Depends(get_billing_service)):
    options = {"upload_id": body.upload_id}
    if body.transcript_id:
        options["transcript_id"] = body.transcript_id
    return success_response(await service.quote(user.id, body.url, body.action, body.preset_id, body.language.lower(), **options))


@router.post("/tasks", status_code=202)
async def confirm(body: ConfirmRequest, user=Depends(required_user), service=Depends(get_billing_service)):
    task = await service.confirm(user.id, body.quote_id)
    return success_response({"task_id": str(task.id), "status": task.status,
                             "events_url": f"/api/v1/tasks/{task.id}/events"})


@router.post("/resume")
async def resume(user=Depends(required_user), service=Depends(get_billing_service)):
    return success_response({"dispatched": await resume_queued(service.tasks.repository, user.id)})
