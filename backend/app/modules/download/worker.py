import asyncio
from app.infrastructure.timing import log_queue_wait, stage_timer
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
from app.infrastructure.storage.retry import wait_for_storage
from app.modules.download.adapter import DownloadAdapter, YtDlpDownloadAdapter
from app.modules.media.repository import MediaRecord, MediaRepository
from app.modules.media.service import safe_filename
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.service import TaskService, database_call
from app.modules.tasks.checkpoints import digest


class DownloadDispatcher:
    def enqueue(self, task_id: UUID) -> None:
        celery_app.send_task("downloads.download", args=[str(task_id)], task_id=str(task_id))

    def enqueue_routed(self, task_id, queue):
        from app.modules.tasks.dispatch import send
        send("downloads.download", task_id, queue)


async def owned_cached_download(task, service, media, storage):
    from types import SimpleNamespace
    from app.modules.media.service import MediaService
    file_limit = task.input_payload.get("allowance", {}).get("limits", {}).get("file_bytes", settings.MAX_FILE_SIZE_MB * 1024 * 1024)
    for candidate in (await database_call(media.reusable, task.video_id, utcnow()))[:32]:
        if candidate.preset_id != task.input_payload["preset_id"] or candidate.size_bytes > file_limit:
            continue
        if not await service.can_read_results("file_id", [candidate.id], SimpleNamespace(id=task.user_id)):
            continue
        if task.input_payload["preset_id"] == "video_source_mp4":
            original = await database_call(service.repository.get, candidate.task_id)
            if original.input_payload.get("source_selection") != task.input_payload.get("source_selection"):
                continue
        try:
            await MediaService(media, storage).download(candidate.id)
        except (AppException, OSError):
            continue
        return {"file_id": str(candidate.id), "filename": candidate.filename,
            "size_bytes": candidate.size_bytes, "expires_at": candidate.expires_at.isoformat(),
            "download_url": f"/api/v1/files/{candidate.id}/download", "delivery_mode": "cached", "cache_hit": True}
    return None


async def execute_download(task_id: UUID, service: TaskService, media: MediaRepository,
                           storage: StorageAdapter, downloader: DownloadAdapter, reuse_result=None,
                           preparer=None, *, claimed=False, defer_render=None, render=False) -> dict:
    repository = service.repository
    if not claimed:
        pending = await database_call(repository.get, task_id)
        cache_ready = False
        if pending.status == "queued" and preparer is not None and settings.ALLOWANCES_ENABLED and pending.user_id:
            cache_ready = bool(await owned_cached_download(pending, service, media, storage))
        if not await database_call(repository.start, task_id, download_cache_ready=cache_ready):
            return {"task_id": str(task_id), "status": (await database_call(repository.get, task_id)).status}
    task = await database_call(repository.get, task_id)
    log_queue_wait(task)
    progress, status = task.progress, task.status
    promoted_key = None
    file_id = uuid4()
    payload = task.input_payload
    download_key = None
    if claimed and not payload.get("upload_id"):
        download_key = digest(["download-v1", str(task.video_id), payload["preset_id"],
                               payload.get("source_selection")])

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
        async def waiting():
            await on_progress({"stage": "waiting_for_storage", "progress": progress})

        if not claimed:
            await cleanup_expired_files(storage, media, repository, is_cancel_requested=cancelled, on_wait=waiting)
        global_limit = settings.MAX_FILE_SIZE_MB * 1024 * 1024
        file_limit = min(global_limit, payload.get("allowance", {}).get("limits", {}).get("file_bytes", global_limit))
        prepare_delivery = preparer is not None and settings.ALLOWANCES_ENABLED and task.user_id
        if prepare_delivery:
            result = await owned_cached_download(task, service, media, storage)
            if result:
                task = await database_call(repository.complete_with, task_id, result, lambda session: None)
                await publish(task)
                return {"task_id": str(task_id), "status": task.status}
            if payload.get("_download_execution_mode") == "cache":
                raise AppException("DOWNLOAD_REQUOTE_REQUIRED", "缓存文件已失效，请重新创建下载", 409)
        if reuse_result is not None or download_key:
            async with wait_for_storage(storage.lease, is_cancel_requested=cancelled, on_wait=waiting):
                if download_key:
                    candidate = await database_call(media.find_download, download_key, utcnow(), file_limit)
                    reused = None
                    if candidate:
                        path = storage.resolve_file(candidate.storage_key)
                        if path.is_file() and path.stat().st_size == candidate.size_bytes:
                            reused = file_result(candidate, reused=True)
                else:
                    reused = await reuse_result(task)
                if reused:
                    task = await database_call(repository.complete_with, task_id, reused, lambda session: None)
                    if render:
                        storage.remove_task_directory(task_id)
                    await publish(task)
                    return {"task_id": str(task_id), "status": task.status}
        if prepare_delivery:
            await on_progress({"stage": "preparing_delivery", "progress": 5})
            from app.modules.download.delivery import prepare_with_cancellation
            source = await prepare_with_cancellation(preparer, task, file_limit, cancelled)
            if source:
                from app.modules.download.repository import DeliveryRepository
                expires = utcnow() + timedelta(minutes=15)
                result = {"delivery_id": str(task_id), "delivery_mode": source["delivery_mode"],
                    "filename": source["filename"], "size_bytes": source["size_bytes"],
                    "expires_at": expires.isoformat(), "range_supported": source["range_supported"]}
                task = await database_call(repository.complete_with, task_id, result,
                    lambda session: DeliveryRepository.register(session, task, source, expires))
                await publish(task)
                return {"task_id": str(task_id), "status": task.status}
        # Input streams and merged output coexist, but admissions need only a short mutex.
        reserve_bytes = (2 if claimed or "allowance" in payload else 1) * file_limit
        async with wait_for_storage(lambda: storage.reservation(task_id, reserve_bytes),
                is_cancel_requested=cancelled, on_wait=waiting):
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            directory = storage.root / "tmp" / str(task_id) if render else storage.create_task_directory(task_id)
            with stage_timer("download_render" if render else "download", task_id):
                inputs = {**payload, "_defer_render": True} if defer_render else payload
                output = await downloader.download(inputs, directory, is_cancel_requested=cancelled,
                    on_progress=on_progress, check_capacity=storage.download_capacity_guard() if claimed else storage.check_capacity)
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            if defer_render:
                await defer_render()
                return {"task_id": str(task_id), "status": "processing"}
            storage.check_capacity()
            extension = "mp3" if task.input_payload["preset_id"] == "audio_mp3" else "mp4"
            size = output.stat().st_size
            if size > file_limit:
                raise AppException("FILE_TOO_LARGE", "成品超过当前权益的单文件上限", 413)
            expires_at = utcnow() + timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)
            async with wait_for_storage(storage.lease, is_cancel_requested=cancelled, on_wait=waiting):
                promoted_key, = storage.promote_batch(task_id, [(output, file_id, extension)], locked=True)
                media_record = MediaRecord(file_id, task_id, task.video_id, task.input_payload["preset_id"], promoted_key,
                                       safe_filename(task.input_payload.get("title", "video"), extension),
                                       "audio/mpeg" if extension == "mp3" else "video/mp4", size, expires_at,
                                       purpose="full_audio" if extension == "mp3" else "full_source",
                                       download_key=download_key)
                task = await database_call(repository.complete_with, task_id, file_result(media_record),
                                           lambda session: media.register(session, media_record))
                if task.status != "completed":
                    storage.remove_file(promoted_key)
                storage.remove_task_directory(task_id)
        await publish(task)
        if task.status == "completed" and promoted_key:
            from app.modules.media.mirror import schedule_mirror
            await schedule_mirror(file_id)
    except Exception as exc:
        # If commit outcome is uncertain, never delete a file belonging to a committed completion.
        current = await database_call(repository.get, task_id)
        if current.status == "completed":
            await publish(current)
            return {"task_id": str(task_id), "status": current.status}
        if current.phase != task.phase:
            # An ambiguous database response can mean the handoff already committed.
            return {"task_id": str(task_id), "status": current.status}
        code = exc.code if isinstance(exc, AppException) else "DOWNLOAD_FAILED"
        message = exc.message if isinstance(exc, AppException) else "视频下载失败"
        task = await database_call(repository.finish, task_id, "cancelled" if code == "TASK_CANCELLED" else "failed",
                                   progress, error_code=code, error_message=message,
                                   expected_execution_id=current.input_payload.get("_execution_id") if claimed else None)
        # The terminal CAS waits for any uncertain completion transaction before
        # the manifest is used to reclaim unregistered files.
        if task.status in {"failed", "cancelled", "expired"} and code != "PROCESS_CLEANUP_FAILED":
            storage.discard_batch(task_id, await database_call(media.storage_keys))
            storage.remove_task_directory(task_id)
        await publish(task)
    return {"task_id": str(task_id), "status": task.status}


def file_result(record, *, reused=False):
    result = {"file_id": str(record.id), "download_url": f"/api/v1/files/{record.id}/download",
              "filename": record.filename, "size_bytes": record.size_bytes, "expires_at": record.expires_at.isoformat()}
    if reused:
        result["reused"] = True
    return result


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
            from app.modules.download.delivery import DeliveryPreparer
            return await execute_download(UUID(task_id), service, MediaRepository(SessionLocal), storage, adapter,
                preparer=DeliveryPreparer())
        finally:
            await redis.aclose()

    return asyncio.run(execute())
