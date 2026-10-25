import asyncio
from dataclasses import asdict
from datetime import timedelta
import time
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import AppException
from app.infrastructure.queue.progress_bus import ProgressBus, ProgressSubscription, TERMINAL
from app.modules.tasks.repository import TaskRecord, TaskRepository, utcnow


class TaskDispatcher(Protocol):
    def enqueue(self, task_id: UUID) -> None: ...


async def database_call(method, *args, **kwargs):
    try:
        async with asyncio.timeout(3):
            return await asyncio.to_thread(method, *args, **kwargs)
    except (SQLAlchemyError, TimeoutError):
        raise AppException("DATABASE_UNAVAILABLE", "任务数据库暂不可用，请稍后重试", 503) from None


class TaskService:
    def __init__(self, repository: TaskRepository, bus: ProgressBus, dispatcher: TaskDispatcher | None = None):
        self.repository = repository
        self.bus = bus
        self.dispatcher = dispatcher

    async def create(self, task_type: str = "simulate_progress", video_id: UUID | None = None,
                     input_payload: dict | None = None) -> TaskRecord:
        if task_type not in {"simulate_progress", "download", "audio_extraction", "transcription", "ai_summary", "ai_mindmap", "ai_question"} or self.dispatcher is None:
            raise AppException("UNSUPPORTED_TASK_TYPE", "不支持该任务类型", 422)
        from app.modules.auth.dependencies import task_user_id
        task = await database_call(self.repository.create, task_type, video_id, input_payload,
                                   utcnow() + timedelta(hours=24), user_id=task_user_id.get())
        try:
            await self.bus.publish(task.id, "queued", 0)
            async with asyncio.timeout(15):
                await asyncio.to_thread(self.dispatcher.enqueue, task.id)
            if "allowance" in task.input_payload:
                await database_call(self.repository.mark_dispatched, task.id)
        except Exception:
            if "allowance" in task.input_payload:
                # Publish/ACK failure is ambiguous: an executing worker may already
                # own the task. Leave its durable state intact for reconciliation.
                return await database_call(self.repository.get, task.id)
            failed = await database_call(self.repository.finish, task.id, "failed", 0,
                                         error_code="TASK_DISPATCH_FAILED", error_message="任务入队失败")
            try:
                await self.bus.publish(task.id, failed.status, failed.progress, error_code=failed.error_code)
            except AppException:
                pass
            raise AppException("TASK_DISPATCH_FAILED", "任务入队失败，请稍后重试", 503) from None
        return task

    async def authorize(self, task_id: UUID, user):
        from app.modules.auth.dependencies import check_owner
        task = await database_call(self.repository.get, task_id)
        check_owner(task.user_id, user)

    async def can_read_results(self, field: str, result_ids: list[UUID], user) -> bool:
        return await database_call(self.repository.has_result_grant, field, result_ids, user.id if user else None)

    async def authorize_artifact(self, field: str, result_id: UUID, source_task_id: UUID | None, user):
        from app.core.config import settings
        if settings.ALLOWANCES_ENABLED:
            if user is not None and await self.can_read_results(field, [result_id], user):
                return
            raise AppException("FORBIDDEN", "只能读取自己已成功获取的结果", 403)
        if source_task_id is None:
            return  # Legacy/imported cache entries have no owning user.
        task = await database_call(self.repository.get, source_task_id)
        if task.user_id is None or (user and task.user_id == user.id):
            return
        if await self.can_read_results(field, [result_id], user):
            return
        raise AppException("FORBIDDEN", "无权读取其他用户的任务结果", 403)

    async def authorize_file(self, file_id: UUID, source_task_id: UUID, user):
        await self.authorize_artifact("file_id", file_id, source_task_id, user)

    async def get(self, task_id: UUID) -> dict:
        task = await database_call(self.repository.get, task_id)
        try:
            snapshot = await self.bus.snapshot(task_id)
            stage = "waiting_for_" + task.wait_reason if task.status == "queued" and task.wait_reason else task.status
            # Heal a missed transition publish, e.g. worker exited after committing PG.
            if snapshot is None or snapshot["status"] != task.status or (task.status == "queued" and snapshot.get("stage") != stage):
                await self.bus.publish(task_id, task.status, task.progress, stage=stage, error_code=task.error_code)
                snapshot = await self.bus.snapshot(task_id)
            if snapshot and snapshot["status"] != task.status:
                task = await database_call(self.repository.get, task_id)
        except AppException:
            snapshot = None  # GET remains usable using durable database state.
        return self.serialize(task, snapshot)

    @staticmethod
    def serialize(task: TaskRecord, snapshot: dict | None = None) -> dict:
        data = asdict(task)
        data.pop("worker_pid", None)
        data.pop("worker_host", None)
        data["task_id"] = str(data.pop("id"))
        data.update(seq=0, stage=task.status, updated_at=(task.finished_at or task.started_at or task.created_at).isoformat())
        if snapshot and snapshot["status"] == task.status:
            data.update(snapshot)
        if task.wait_reason and task.status == "queued":
            data["stage"] = "waiting_for_" + task.wait_reason
        return data

    async def cancel(self, task_id: UUID) -> dict:
        async def persist():
            task = await database_call(self.repository.request_cancel, task_id)
            return task.cancel_requested

        await self.bus.request_task_cancel(task_id, persist)
        return await self.get(task_id)

    async def is_cancel_requested(self, task_id: UUID) -> bool:
        async def persisted():
            return (await database_call(self.repository.get, task_id)).cancel_requested

        return await self.bus.is_cancel_requested(task_id, persisted)

    async def open_events(self, task_id: UUID) -> tuple[ProgressSubscription, dict]:
        subscription = await self.bus.subscribe(task_id)
        try:
            # Nothing may read the initial Redis/PG snapshot before subscribe ACK.
            initial = await self.get(task_id)
            if initial["seq"] == 0:
                raise AppException("PROGRESS_UNAVAILABLE", "任务进度服务暂不可用，请稍后重试", 503)
            return subscription, self.event(initial)
        except BaseException:
            await subscription.close()
            raise

    @staticmethod
    def event(data: dict) -> dict:
        return {key: data[key] for key in ("task_id", "status", "progress", "seq", "stage", "error_code", "updated_at")}

    async def events(self, task_id: UUID, subscription: ProgressSubscription, initial: dict, check_access=None):
        last_seq = initial["seq"]
        last_refresh = last_heartbeat = started = time.monotonic()
        try:
            if check_access:
                await check_access()
            yield initial
            if initial["status"] in TERMINAL:
                return
            while time.monotonic() - started < 300:
                if check_access:
                    await check_access()
                event = await subscription.receive()
                now = time.monotonic()
                if event is None and now - last_refresh >= 1:
                    data = await self.get(task_id)
                    if data["seq"] == 0:
                        raise AppException("PROGRESS_UNAVAILABLE", "任务进度服务暂不可用，请重新连接", 503)
                    event = self.event(data)
                    last_refresh = now
                if event and event["seq"] > last_seq:
                    last_seq = event["seq"]
                    yield event
                    if event["status"] in TERMINAL:
                        return
                elif now - last_heartbeat >= 15:
                    yield None
                    last_heartbeat = now
        finally:
            await subscription.close()
