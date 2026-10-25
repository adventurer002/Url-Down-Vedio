import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.modules.auth.dependencies import optional_user, required_user, get_auth_service
from app.core.exceptions import AppException
from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


def get_task_service() -> TaskService:
    return TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client))


def get_history_service():
    from app.modules.tasks.history import TaskHistoryService
    from app.modules.video.repository import VideoRepository
    from app.modules.download.router import get_media_service
    return TaskHistoryService(get_task_service(), VideoRepository(SessionLocal), get_media_service())


@router.get("")
async def list_tasks(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
                     service=Depends(get_history_service), user=Depends(optional_user)):
    if user is None:
        return success_response({"items": [], "total": 0, "limit": limit, "offset": offset})
    return success_response(await service.list(limit, offset, user_id=user.id))


@router.delete("/history")
async def clear_history(service=Depends(get_history_service), user=Depends(required_user)):
    return success_response(await service.delete(user.id))


@router.delete("/history/{task_id}")
async def delete_history(task_id: UUID, service=Depends(get_history_service), user=Depends(required_user)):
    return success_response(await service.delete(user.id, task_id))


@router.get("/{task_id}")
async def get_task(task_id: UUID, service: TaskService = Depends(get_task_service), user=Depends(optional_user)):
    await service.authorize(task_id, user)
    return success_response(await service.get(task_id))


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: UUID, service: TaskService = Depends(get_task_service), user=Depends(optional_user)):
    await service.authorize(task_id, user)
    return success_response(await service.cancel(task_id))


@router.get("/{task_id}/events")
async def task_events(task_id: UUID, request: Request, service: TaskService = Depends(get_task_service),
                      user=Depends(optional_user), auth=Depends(get_auth_service)):
    await service.authorize(task_id, user)
    subscription, initial = await service.open_events(task_id)

    async def check_access():
        if user is not None:
            await auth.user(user.id)

    async def stream():
        try:
            async for event in service.events(task_id, subscription, initial, check_access):
                if event is None:
                    yield ": heartbeat\n\n"
                else:
                    yield f"id: {event['seq']}\nevent: progress\ndata: {json.dumps(event)}\n\n"
        except AppException as exc:
            error = {"error": {"code": exc.code, "message": exc.message, "request_id": request.state.request_id}}
            yield f"event: error\ndata: {json.dumps(error)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                             background=BackgroundTask(subscription.close))
