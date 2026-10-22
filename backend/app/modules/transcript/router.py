from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.service import TranscriptService
from app.modules.transcript.worker import TranscriptDispatcher
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.repository import VideoRepository
from app.modules.video.resolver import VideoResolver

router = APIRouter(prefix="/transcriptions", tags=["transcriptions"])


class TranscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=32)


def get_transcript_service():
    return TranscriptService(VideoResolver(YtDlpParser(), VideoRepository(SessionLocal)),
                             TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client), TranscriptDispatcher()),
                             TranscriptRepository(SessionLocal))


@router.post("", status_code=202)
async def create_transcription(body: TranscriptionRequest, service: TranscriptService = Depends(get_transcript_service)):
    task = await service.create(body.url, body.language)
    return success_response({"task_id": str(task.id), "status": task.status})


@router.get("/{transcript_id}")
async def get_transcription(transcript_id: UUID, service: TranscriptService = Depends(get_transcript_service)):
    return success_response(await service.get(transcript_id))
