from app.modules.auth.dependencies import creation_user, optional_user
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.infrastructure.storage import LocalStorageAdapter
from app.modules.download.service import DownloadService
from app.modules.download.worker import DownloadDispatcher
from app.modules.media.repository import MediaRepository
from app.modules.media.service import MediaService
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.repository import VideoRepository

router = APIRouter(tags=["downloads"])


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    preset_id: str = Field(min_length=1, max_length=64)


def get_storage():
    return LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)


def get_download_service() -> DownloadService:
    tasks = TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client), DownloadDispatcher())
    return DownloadService(YtDlpParser(), VideoRepository(SessionLocal), tasks, get_storage())


def get_media_service() -> MediaService:
    return MediaService(MediaRepository(SessionLocal), get_storage())


def get_file_task_service(media=Depends(get_media_service)):
    return TaskService(TaskRepository(media.repository.sessions), ProgressBus(redis_client))


@router.post("/downloads", status_code=202)
async def create_download(body: DownloadRequest, service: DownloadService = Depends(get_download_service), user=Depends(creation_user)):
    task = await service.create(body.url, body.preset_id)
    return success_response({"task_id": str(task.id), "status": task.status,
                             "events_url": f"/api/v1/tasks/{task.id}/events"})


@router.get("/files/{file_id}/download")
async def download_file(file_id: UUID, service: MediaService = Depends(get_media_service),
                        tasks=Depends(get_file_task_service), user=Depends(optional_user)):
    record, path, filename = await service.download(file_id)
    await tasks.authorize_file(file_id, record.task_id, user)
    return FileResponse(path, filename=filename, media_type=record.mime_type,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
