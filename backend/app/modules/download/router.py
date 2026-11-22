from app.modules.auth.dependencies import creation_user, required_user, resource_user
from app.modules.auth.tokens import issue_resource_access
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
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
from app.modules.video.parsers.cached import get_video_parser
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
    return DownloadService(get_video_parser(), VideoRepository(SessionLocal), tasks, get_storage())


def get_media_service() -> MediaService:
    return MediaService(MediaRepository(SessionLocal), get_storage())


def get_file_task_service(media=Depends(get_media_service)):
    return TaskService(TaskRepository(media.repository.sessions), ProgressBus(redis_client))


def get_delivery_service():
    from app.modules.download.delivery import delivery_service
    return delivery_service(SessionLocal)


@router.post("/deliveries/{delivery_id}/download/access")
async def delivery_access(delivery_id: UUID, user=Depends(required_user), service=Depends(get_delivery_service)):
    from app.modules.tasks.service import database_call
    await service.authorize(delivery_id, user)
    await database_call(service.repository.validate_access, delivery_id, user.id)
    return JSONResponse(success_response(issue_resource_access(user.id, user.auth_version, "delivery_download", delivery_id, lifetime=900)),
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@router.get("/deliveries/{delivery_id}")
async def delivery_status(delivery_id: UUID, user=Depends(required_user), service=Depends(get_delivery_service)):
    from app.modules.tasks.service import database_call
    await service.authorize(delivery_id, user, allow_expired=True)
    return JSONResponse(jsonable_encoder(success_response(await database_call(service.repository.get, delivery_id, user.id))),
        headers={"Cache-Control": "private, no-store"})


@router.get("/deliveries/{delivery_id}/download")
async def delivery_download(delivery_id: UUID, request: Request, user=Depends(resource_user),
                            service=Depends(get_delivery_service)):
    if user is None:
        from app.core.exceptions import AppException
        raise AppException("AUTH_REQUIRED", "请先登录后下载", 401)
    return await service.response(delivery_id, user, request)


@router.post("/deliveries/{delivery_id}/fallback", status_code=202)
async def delivery_fallback(delivery_id: UUID, user=Depends(required_user), service=Depends(get_delivery_service)):
    task = await service.fallback(delivery_id, user)
    return success_response({"task_id": str(task.id), "status": task.status})


@router.post("/downloads", status_code=202)
async def create_download(body: DownloadRequest, service: DownloadService = Depends(get_download_service), user=Depends(creation_user)):
    task = await service.create(body.url, body.preset_id)
    return success_response({"task_id": str(task.id), "status": task.status,
                             "events_url": f"/api/v1/tasks/{task.id}/events"})


@router.get("/files/{file_id}/download")
async def download_file(file_id: UUID, request: Request, service: MediaService = Depends(get_media_service),
                        tasks=Depends(get_file_task_service), user=Depends(resource_user)):
    record, filename = await service.download_record(file_id)
    await tasks.authorize_file(file_id, record.task_id, user)
    if settings.ALLOWANCES_ENABLED:
        from app.modules.billing.repository import AllowanceRepository
        from app.modules.billing.transfer import AllowanceFileResponse, transfer_length
        from app.modules.media.delivery import redirect_download
        from app.modules.tasks.service import database_call
        repository = AllowanceRepository(service.repository.sessions)
        redirect = await redirect_download(record, filename, request, user, repository, service.repository)
        if redirect is not None:
            return redirect
        path = service.local_download_path(record)
        length = transfer_length(record.size_bytes, request.headers.get("range"), request.headers.get("if-range"))
        transfer_id = await database_call(repository.reserve_transfer, user.id, length)
        return AllowanceFileResponse(path, filename=filename, media_type=record.mime_type,
            repository=repository, transfer_id=transfer_id,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
    return FileResponse(service.local_download_path(record), filename=filename, media_type=record.mime_type,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.post("/files/{file_id}/download/access")
async def download_access(file_id: UUID, service: MediaService = Depends(get_media_service),
                          tasks=Depends(get_file_task_service), user=Depends(required_user)):
    from app.modules.media.delivery import delivery_destination

    record, filename = await service.download_record(file_id)
    await tasks.authorize_file(file_id, record.task_id, user)
    if not delivery_destination(record, filename):
        service.local_download_path(record)
    return JSONResponse(success_response(issue_resource_access(user.id, user.auth_version, "file_download", file_id)),
                        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
