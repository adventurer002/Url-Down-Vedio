from app.modules.auth.dependencies import creation_user, optional_user
from uuid import UUID
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.ai.repository import AIRepository
from app.modules.ai.service import AIService
from app.modules.ai.worker import AIDispatcher
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService
from app.modules.video.parsers.cached import get_video_parser
from app.modules.video.repository import VideoRepository
from app.modules.video.resolver import VideoResolver

router = APIRouter(prefix="/ai", tags=["ai"])

from app.modules.video.sources import AIInputRequest


class AIRequest(AIInputRequest):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="zh", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=32)

def get_ai_service():
    return AIService(VideoResolver(get_video_parser(), VideoRepository(SessionLocal)),
        TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client), AIDispatcher()), AIRepository(SessionLocal))

@router.post("/summaries", status_code=202)
async def create_summary(body: AIRequest, service: AIService = Depends(get_ai_service), user=Depends(creation_user)):
    options = {"upload_id": body.upload_id, "user_id": user.id if user else None} if body.upload_id else {}
    if body.transcript_id:
        options = {"transcript_id": body.transcript_id, "user_id": user.id if user else None}
    task = await service.create(body.url, body.language, "summary", **options)
    return success_response({"task_id": str(task.id), "status": task.status})

@router.post("/mindmaps", status_code=202)
async def create_mindmap(body: AIRequest, service: AIService = Depends(get_ai_service), user=Depends(creation_user)):
    options = {"upload_id": body.upload_id, "user_id": user.id if user else None} if body.upload_id else {}
    if body.transcript_id:
        options = {"transcript_id": body.transcript_id, "user_id": user.id if user else None}
    task = await service.create(body.url, body.language, "mindmap", **options)
    return success_response({"task_id": str(task.id), "status": task.status})

@router.get("/results/{result_id}")
async def get_result(result_id: UUID, service: AIService = Depends(get_ai_service), user=Depends(optional_user)):
    return success_response(await service.get(result_id, user))
