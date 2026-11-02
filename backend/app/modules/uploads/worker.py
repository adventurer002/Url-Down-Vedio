import asyncio
from uuid import UUID

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.storage.retry import wait_for_storage
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.uploads.probe import FFprobeAdapter, MediaProbe, validate_probe
from app.modules.uploads.service import upload_service


class UploadDispatcher:
    def enqueue(self, task_id):
        celery_app.send_task("uploads.probe", args=[str(task_id)], task_id=str(task_id))


async def execute_probe(task_id, tasks, uploads, probe: MediaProbe):
    repository = tasks.repository
    if not await database_call(repository.start, task_id):
        return {"task_id": str(task_id), "status": (await database_call(repository.get, task_id)).status}
    task = await database_call(repository.get, task_id)
    identifier = UUID(task.input_payload["upload_id"])
    keep_directory = False

    async def cancelled():
        return await tasks.is_cancel_requested(task_id)

    async def progress(event):
        await tasks.bus.publish(task_id, "processing", 30, stage="media_probe")

    try:
        await database_call(repository.mark_processing, task_id)
        await progress({})
        with uploads.files.lock(identifier, shared=True):
            item = await database_call(uploads.repository.get, identifier, task.user_id)
            from types import SimpleNamespace
            uploads.repository.available(SimpleNamespace(**item), {"validating"})
            async def waiting():
                await tasks.bus.publish(task_id, "processing", 30, stage="waiting_for_storage")

            async with wait_for_storage(lambda: uploads.storage.reservation(task_id, 1024 * 1024),
                    is_cancel_requested=cancelled, on_wait=waiting):
                directory = uploads.storage.create_task_directory(task_id)
                info = await probe.inspect(uploads.files.valid_file(item), directory, is_cancel_requested=cancelled,
                    on_progress=progress, check_capacity=uploads.storage.check_capacity)
                metadata = validate_probe(info.get("probe", {}), item["extension"])
                limits = await database_call(uploads.repository.resource_limits, task.user_id)
                if metadata["duration_seconds"] > limits["video_seconds"]:
                    raise AppException("VIDEO_TOO_LONG", "音频时长超过当前账号的处理上限", 422)
                if item["size_bytes"] > min(limits["file_bytes"], settings.MAX_FILE_SIZE_MB * 1024**2):
                    raise AppException("FILE_TOO_LARGE", "文件超过当前账号的处理体积上限", 413)
                task = await database_call(repository.complete_with, task_id, {}, lambda session:
                    uploads.repository.ready(session, identifier, task.user_id, task_id, **metadata))
    except Exception as exc:
        keep_directory = isinstance(exc, AppException) and exc.code == "PROCESS_CLEANUP_FAILED"
        code = exc.code if isinstance(exc, AppException) else "UPLOAD_PROBE_FAILED"
        message = exc.message if isinstance(exc, AppException) else "媒体文件校验失败"
        task = await database_call(repository.finish, task_id, "cancelled" if code == "TASK_CANCELLED" else "failed", 0,
            error_code=code, error_message=message)
    finally:
        if not keep_directory:
            uploads.storage.remove_task_directory(task_id)
    if task.status != "completed":
        await database_call(uploads.repository.terminal, identifier, task.user_id, "failed", task.error_code)
        if not keep_directory:
            with uploads.files.lock(identifier):
                uploads.files.remove(identifier)
                await database_call(uploads.repository.mark_purged, identifier, task.user_id)
    try:
        await tasks.bus.publish(task_id, task.status, task.progress, error_code=task.error_code)
    except AppException:
        pass
    return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="uploads.probe")
def probe_task(task_id):
    async def run():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
            socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            tasks = TaskService(TaskRepository(SessionLocal), ProgressBus(redis))
            uploads = upload_service(SessionLocal, tasks=tasks)
            return await execute_probe(UUID(task_id), tasks, uploads, FFprobeAdapter(
                ProcessRunner(settings.UPLOAD_PROBE_TIMEOUT_SECONDS, 1024 * 1024)))
        finally:
            await redis.aclose()
    return asyncio.run(run())
