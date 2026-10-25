from app.modules.auth.dependencies import creation_user
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.media.worker import AudioDispatcher
from app.modules.media.service import AudioExtractionService
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.repository import VideoRepository
from app.modules.video.resolver import VideoResolver

router = APIRouter(prefix="/audio", tags=["audio"])


class AudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    format: Literal["mp3"] = "mp3"


def get_audio_service():
    return AudioExtractionService(VideoResolver(YtDlpParser(), VideoRepository(SessionLocal)),
                                  TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client), AudioDispatcher()))


@router.post("/extractions", status_code=202)
async def create_extraction(body: AudioRequest, service: AudioExtractionService = Depends(get_audio_service), user=Depends(creation_user)):
    task = await service.create(body.url)
    return success_response({"task_id": str(task.id), "status": task.status})
