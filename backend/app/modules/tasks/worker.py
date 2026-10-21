import asyncio
import time
from uuid import UUID

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call


class CeleryTaskDispatcher:
    def enqueue(self, task_id: UUID) -> None:
        celery_app.send_task("tasks.simulate_progress", args=[str(task_id)], task_id=str(task_id))


async def simulate(service: TaskService, task_id: UUID) -> dict:
    repository = service.repository
    progress = 0

    async def publish(task):
        await service.bus.publish(task_id, task.status, task.progress, error_code=task.error_code)

    if not await database_call(repository.start, task_id):
        task = await database_call(repository.get, task_id)
        return {"task_id": str(task_id), "status": task.status}
    try:
        # Total budget includes I/O, not just the 3-second demonstration sleep.
        async with asyncio.timeout(15):
            await publish(await database_call(repository.get, task_id))
            started = time.monotonic()
            for step in range(1, 7):
                await asyncio.sleep(max(0, started + step * 0.5 - time.monotonic()))
                if await service.is_cancel_requested(task_id):
                    task = await database_call(repository.finish, task_id, "cancelled", progress)
                    await publish(task)
                    return {"task_id": str(task_id), "status": task.status}
                progress = round(step / 6 * 100)
                if step == 4:
                    if await database_call(repository.mark_processing, task_id):
                        await service.bus.publish(task_id, "processing", progress)
                if step < 6:
                    task = await database_call(repository.get, task_id)
                    await service.bus.publish(task_id, task.status, progress)
            task = await database_call(repository.finish, task_id, "completed", 100,
                                       result_payload={"simulated": True})
            await publish(task)
            return {"task_id": str(task_id), "status": task.status}
    except Exception as exc:
        code = "TASK_TIMEOUT" if isinstance(exc, TimeoutError) else "TASK_EXECUTION_FAILED"
        task = await database_call(repository.finish, task_id, "failed", progress,
                                   error_code=code, error_message="演示任务执行失败")
        try:
            await publish(task)
        except AppException:
            pass
        return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="tasks.simulate_progress")
def simulate_progress(task_id: str) -> dict:
    async def execute():
        # A fresh async Redis client per Solo task avoids cross-event-loop reuse.
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                               socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            service = TaskService(TaskRepository(SessionLocal), ProgressBus(redis))
            return await simulate(service, UUID(task_id))
        finally:
            await redis.aclose()

    return asyncio.run(execute())


async def create_demo() -> UUID:
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                           socket_timeout=1, retry=Retry(NoBackoff(), 0))
    try:
        service = TaskService(TaskRepository(SessionLocal), ProgressBus(redis), CeleryTaskDispatcher())
        return (await service.create()).id
    finally:
        await redis.aclose()


if __name__ == "__main__":
    print(asyncio.run(create_demo()))
