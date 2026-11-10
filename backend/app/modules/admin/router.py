import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.modules.admin.service import AdminService
from app.modules.auth.dependencies import admin_writer, required_admin
from app.modules.tasks.service import database_call
from app.modules.tasks.models import TaskStatus
from app.modules.tasks.router import get_task_service
from app.modules.system.service import get_cleanup_service, get_storage_status_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(required_admin)])


def get_admin_service():
    return AdminService(SessionLocal)


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=500)
    key: str = Field(min_length=1, max_length=128)


class StatusRequest(OperationRequest):
    status: Literal["active", "disabled"]
    expected_status: Literal["active", "disabled"]
    expected_version: int = Field(ge=0, strict=True)


class GrantRequest(OperationRequest):
    product: Literal["monthly", "asr_120", "ai_100"]


TaskType = Literal["download", "audio_extraction", "transcription", "ai_summary", "ai_mindmap", "ai_question", "ai_clip_plan", "clips_export", "media_probe", "simulate_progress", "model_test"]


async def add_progress(page, tasks):
    async def snapshot(item):
        try:
            current = await tasks.bus.snapshot(item["task_id"])
            if current and current.get("status") == item["status"] and current.get("task_id") == str(item["task_id"]):
                return current
        except Exception:
            pass
        return None
    try:
        async with asyncio.timeout(1.5):
            snapshots = await asyncio.gather(*(snapshot(item) for item in page["items"]))
    except TimeoutError:
        snapshots = [None] * len(page["items"])
    for item, current in zip(page["items"], snapshots):
        item["seq"] = current.get("seq", 0) if current else 0
        if current:
            item["progress"] = current["progress"]
        item["stage"] = "waiting_for_" + item["wait_reason"] if item["status"] == "queued" and item["wait_reason"] else current.get("stage", item["status"]) if current else item["status"]
    return page


@router.get("/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                     user_id: UUID | None = None, status: TaskStatus | None = None, task_type: TaskType | None = None,
                     since: AwareDatetime | None = None, until: AwareDatetime | None = None,
                     service=Depends(get_admin_service), tasks=Depends(get_task_service)):
    page = await database_call(service.list_tasks, limit, offset, user_id, status, task_type, since, until)
    return success_response(await add_progress(page, tasks))


@router.get("/tasks/{task_id}")
async def task_detail(task_id: UUID, service=Depends(get_admin_service), tasks=Depends(get_task_service)):
    item = await database_call(service.task_detail, task_id)
    return success_response((await add_progress({"items": [item]}, tasks))["items"][0])


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: UUID, body: OperationRequest, request: Request, actor=Depends(admin_writer),
                      service=Depends(get_admin_service), tasks=Depends(get_task_service)):
    return success_response(await tasks.cancel_admin(task_id,
        lambda: service.cancel_task(actor, task_id, body.reason, body.key, request.state.request_id)))


@router.get("/audit-events")
async def audit_events(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
    actor_id: UUID | None = None, action: str | None = Query(None, max_length=64),
    target_type: Literal["user", "task", "storage", "model"] | None = None,
    target_id: str | None = Query(None, max_length=128),
    outcome: Literal["succeeded", "accepted", "unchanged", "failed", "unknown"] | None = None,
    since: AwareDatetime | None = None, until: AwareDatetime | None = None, service=Depends(get_admin_service)):
    return success_response(await database_call(service.list_audit, limit, offset, actor_id, action,
        target_type, target_id, outcome, since, until))


@router.get("/overview")
async def overview(service=Depends(get_admin_service)):
    return success_response(await database_call(service.overview))


@router.get("/storage")
async def storage(service=Depends(get_storage_status_service)):
    try:
        async with asyncio.timeout(2):
            result = await asyncio.to_thread(service.snapshot)
    except (OSError, TimeoutError):
        from app.core.exceptions import AppException
        raise AppException("STORAGE_STATUS_UNAVAILABLE", "存储状态暂不可用", 503) from None
    return success_response(result)


@router.post("/storage/cleanup")
async def cleanup(body: OperationRequest, request: Request, actor=Depends(admin_writer),
                   service=Depends(get_admin_service), cleaner=Depends(get_cleanup_service)):
    return success_response(await service.cleanup(actor, body.reason, body.key, request.state.request_id, cleaner))


@router.get("/users/{user_id}/grants/preview")
async def grant_preview(user_id: UUID, product: Literal["monthly", "asr_120", "ai_100"],
                        service=Depends(get_admin_service)):
    return success_response(await database_call(service.grant_preview, user_id, product))


@router.post("/users/{user_id}/grants")
async def grant(user_id: UUID, body: GrantRequest, request: Request,
                actor=Depends(admin_writer), service=Depends(get_admin_service)):
    result = await database_call(service.grant_user, actor, user_id, body.product,
                                 body.reason, body.key, request.state.request_id)
    return success_response(await service.wake_after_grant(user_id, result))


@router.post("/users/{user_id}/status")
async def change_status(user_id: UUID, body: StatusRequest, request: Request,
                        actor=Depends(admin_writer), service=Depends(get_admin_service)):
    return success_response(await database_call(service.change_user_status, actor, user_id, body.status,
        body.expected_status, body.expected_version, body.reason, body.key, request.state.request_id))


@router.get("/users")
async def users(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                q: str | None = Query(None, max_length=100), status: Literal["active", "disabled"] | None = None,
                subscribed: bool | None = None, member: bool | None = None, service=Depends(get_admin_service)):
    return success_response(await database_call(service.list_users, limit, offset, q, status, subscribed, member))


@router.get("/users/{user_id}")
async def user_detail(user_id: UUID, service=Depends(get_admin_service)):
    return success_response(await database_call(service.user_detail, user_id))


@router.get("/users/{user_id}/allowances")
async def allowances(user_id: UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                     service=Depends(get_admin_service)):
    return success_response(await database_call(service.user_allowances, user_id, limit, offset))


@router.get("/users/{user_id}/ledger")
async def ledger(user_id: UUID, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                 service=Depends(get_admin_service)):
    return success_response(await database_call(service.user_ledger, user_id, limit, offset))
