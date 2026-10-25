import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.storage import LocalStorageAdapter, StorageAdapter
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.modules.download.adapter import DownloadAdapter, YtDlpDownloadAdapter
from app.modules.media.repository import MediaRecord, MediaRepository
from app.modules.media.service import safe_filename
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.service import TaskService, database_call


class DownloadDispatcher:
    def enqueue(self, task_id: UUID) -> None:
        celery_app.send_task("downloads.download", args=[str(task_id)], task_id=str(task_id))


async def execute_download(task_id: UUID, service: TaskService, media: MediaRepository,
                           storage: StorageAdapter, downloader: DownloadAdapter, reuse_result=None) -> dict:
    repository = service.repository
    if not await database_call(repository.start, task_id):
        return {"task_id": str(task_id), "status": (await database_call(repository.get, task_id)).status}
    task = await database_call(repository.get, task_id)
    progress, status = 0, "running"
    promoted_key = None
    file_id = uuid4()

    async def publish(record):
        try:
            await service.bus.publish(task_id, record.status, record.progress, error_code=record.error_code)
        except AppException:
            pass  # PG remains authoritative; GET/SSE repairs missed state publishes.

    async def on_progress(event):
        nonlocal progress, status
        progress = max(progress, min(99, event["progress"]))
        if event.get("stage") == "processing" and status == "running":
            if await database_call(repository.mark_processing, task_id):
                status = "processing"
        try:
            await service.bus.publish(task_id, status, progress, stage=event.get("stage"))
        except AppException:
            pass

    async def cancelled():
        return await service.is_cancel_requested(task_id)

    await publish(task)
    try:
        await cleanup_expired_files(storage, media, repository)
        with storage.lease():
            if reuse_result is not None:
                reused = await reuse_result(task)
                if reused:
                    task = await database_call(repository.complete_with, task_id, reused, lambda session: None)
                    await publish(task)
                    return {"task_id": str(task_id), "status": task.status}
            file_limit = task.input_payload.get("allowance", {}).get("limits", {}).get("file_bytes", settings.MAX_FILE_SIZE_MB * 1024 * 1024)
            # Input streams and merged output coexist. Keep the existing global lease.
            storage.check_capacity((2 if "allowance" in task.input_payload else 1) * file_limit)
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            directory = storage.create_task_directory(task_id)
            output = await downloader.download(task.input_payload, directory, is_cancel_requested=cancelled,
                                               on_progress=on_progress, check_capacity=storage.check_capacity)
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            storage.check_capacity()
            extension = "mp3" if task.input_payload["preset_id"] == "audio_mp3" else "mp4"
            size = output.stat().st_size
            if size > file_limit:
                raise AppException("FILE_TOO_LARGE", "成品超过当前权益的单文件上限", 413)
            promoted_key = storage.promote(task_id, output, file_id, extension)
            expires_at = utcnow() + timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)
            media_record = MediaRecord(file_id, task_id, task.video_id, task.input_payload["preset_id"], promoted_key,
                                       safe_filename(task.input_payload.get("title", "video"), extension),
                                       "audio/mpeg" if extension == "mp3" else "video/mp4", size, expires_at)
            result = {"file_id": str(file_id), "download_url": f"/api/v1/files/{file_id}/download",
                      "filename": media_record.filename, "size_bytes": size, "expires_at": expires_at.isoformat()}
            task = await database_call(repository.complete_with, task_id, result,
                                       lambda session: media.register(session, media_record))
            if task.status != "completed":
                storage.remove_file(promoted_key)
            storage.remove_task_directory(task_id)
        await publish(task)
    except Exception as exc:
        if not isinstance(exc, AppException) or exc.code != "PROCESS_CLEANUP_FAILED":
            storage.remove_task_directory(task_id)
        # If commit outcome is uncertain, never delete a file belonging to a committed completion.
        current = await database_call(repository.get, task_id)
        if current.status == "completed":
            await publish(current)
            return {"task_id": str(task_id), "status": current.status}
        if promoted_key:
            storage.remove_file(promoted_key)
        code = exc.code if isinstance(exc, AppException) else "DOWNLOAD_FAILED"
        message = exc.message if isinstance(exc, AppException) else "视频下载失败"
        task = await database_call(repository.finish, task_id, "cancelled" if code == "TASK_CANCELLED" else "failed",
                                   progress, error_code=code, error_message=message)
        await publish(task)
    return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="downloads.download")
def download_task(task_id: str) -> dict:
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                               socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            storage = LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
            service = TaskService(TaskRepository(SessionLocal), ProgressBus(redis))
            adapter = YtDlpDownloadAdapter(ProcessRunner(settings.DOWNLOAD_TIMEOUT_SECONDS,
                                                       settings.MAX_FILE_SIZE_MB * 1024 * 1024))
            return await execute_download(UUID(task_id), service, MediaRepository(SessionLocal), storage, adapter)
        finally:
            await redis.aclose()

    return asyncio.run(execute())
