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
        if task_type not in {"simulate_progress", "download"} or self.dispatcher is None:
            raise AppException("UNSUPPORTED_TASK_TYPE", "不支持该任务类型", 422)
        task = await database_call(self.repository.create, task_type, video_id, input_payload, utcnow() + timedelta(hours=24))
        try:
            await self.bus.publish(task.id, "queued", 0)
            async with asyncio.timeout(15):
                await asyncio.to_thread(self.dispatcher.enqueue, task.id)
        except Exception:
            failed = await database_call(self.repository.finish, task.id, "failed", 0,
                                         error_code="TASK_DISPATCH_FAILED", error_message="任务入队失败")
            try:
                await self.bus.publish(task.id, failed.status, failed.progress, error_code=failed.error_code)
            except AppException:
                pass
            raise AppException("TASK_DISPATCH_FAILED", "任务入队失败，请稍后重试", 503) from None
        return task

    async def get(self, task_id: UUID) -> dict:
        task = await database_call(self.repository.get, task_id)
        try:
            snapshot = await self.bus.snapshot(task_id)
            # Heal a missed transition publish, e.g. worker exited after committing PG.
            if snapshot is None or snapshot["status"] != task.status:
                await self.bus.publish(task_id, task.status, task.progress, error_code=task.error_code)
                snapshot = await self.bus.snapshot(task_id)
            if snapshot and snapshot["status"] != task.status:
                task = await database_call(self.repository.get, task_id)
        except AppException:
            snapshot = None  # GET remains usable using durable database state.
        data = asdict(task)
        data["task_id"] = str(data.pop("id"))
        data.update(seq=0, stage=task.status, updated_at=(task.finished_at or task.started_at or task.created_at).isoformat())
        if snapshot and snapshot["status"] == task.status:
            data.update(snapshot)
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

    async def events(self, task_id: UUID, subscription: ProgressSubscription, initial: dict):
        last_seq = initial["seq"]
        last_refresh = last_heartbeat = started = time.monotonic()
        try:
            yield initial
            if initial["status"] in TERMINAL:
                return
            while time.monotonic() - started < 300:
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
