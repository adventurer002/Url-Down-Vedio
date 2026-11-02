from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from app.core.exceptions import AppException
from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.auth.dependencies import required_user
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService
from app.modules.uploads.service import upload_service
from app.modules.uploads.worker import UploadDispatcher

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0, strict=True)


def get_upload_service():
    return upload_service(SessionLocal, tasks=TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client), UploadDispatcher()))


def public(item):
    return {k: v for k, v in item.items() if k not in {"user_id", "extension"}}


@router.post("", status_code=201)
async def create_upload(body: UploadRequest, user=Depends(required_user), service=Depends(get_upload_service)):
    return success_response(public(await service.create(user.id, body.filename, body.size_bytes)))


@router.put("/{upload_id}/content", openapi_extra={"requestBody": {"required": True,
    "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
async def upload_content(upload_id: UUID, request: Request, user=Depends(required_user), service=Depends(get_upload_service)):
    try:
        length = request.headers.get("content-length")
        length = int(length) if length is not None else None
        if length is not None and length < 0:
            raise ValueError
    except ValueError:
        raise AppException("UPLOAD_SIZE_INVALID", "Content-Length 无效", 422) from None
    try:
        return success_response(public(await service.receive(upload_id, user.id, request.stream(), length)))
    except ClientDisconnect:
        raise AppException("UPLOAD_INTERRUPTED", "上传已中断，请重新上传", 400) from None


@router.post("/{upload_id}/complete", status_code=202)
async def complete_upload(upload_id: UUID, user=Depends(required_user), service=Depends(get_upload_service)):
    return success_response(await service.complete(upload_id, user.id))


@router.get("/{upload_id}")
async def get_upload(upload_id: UUID, user=Depends(required_user), service=Depends(get_upload_service)):
    return success_response(public(await service.get(upload_id, user.id)))


@router.delete("/{upload_id}")
async def delete_upload(upload_id: UUID, user=Depends(required_user), service=Depends(get_upload_service)):
    return success_response(public(await service.delete(upload_id, user.id)))
