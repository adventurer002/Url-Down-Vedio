import asyncio
from uuid import UUID

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
from app.modules.media.audio import AudioAcquirer
from app.modules.media.repository import MediaRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.tasks.repository import TaskRepository
from app.modules.transcript.data import make_transcript
from app.modules.transcript.providers import ASRProvider, get_asr_provider
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.subtitles import SubtitleProvider, YtDlpSubtitleProvider


class TranscriptDispatcher:
    def enqueue(self, task_id: UUID):
        celery_app.send_task("transcripts.transcribe", args=[str(task_id)], task_id=str(task_id))


def result(record, cache_hit=False):
    return {"transcript_id": str(record.id), "source_type": record.source_type,
            "content_hash": record.content_hash, "cache_hit": cache_hit}


async def cooperative_asr(provider: ASRProvider, audio_path, language, cancelled):
    operation = asyncio.create_task(provider.transcribe(audio_path, language))
    try:
        async with asyncio.timeout(settings.ASR_TOTAL_TIMEOUT_SECONDS):
            while not operation.done():
                if await cancelled():
                    raise AppException("TASK_CANCELLED", "任务已取消", 409)
                await asyncio.wait({operation}, timeout=0.5)
            return await operation
    except TimeoutError:
        raise AppException("ASR_TIMEOUT", "ASR 超过总执行时限", 504) from None
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def execute_transcription(task_id: UUID, service: TaskService, transcripts: TranscriptRepository,
                                 media: MediaRepository, storage: StorageAdapter, subtitles: SubtitleProvider,
                                 audio: AudioAcquirer, asr: ASRProvider, *, intermediate=False, intermediate_progress=None):
    tasks = service.repository
    if not intermediate and not await database_call(tasks.start, task_id):
        return {"task_id": str(task_id), "status": (await database_call(tasks.get, task_id)).status}
    task = await database_call(tasks.get, task_id)
    if "allowance" in task.input_payload:
        asr.model = task.input_payload["allowance"]["asr_model"]
    language = task.input_payload.get("language")
    version = task.input_payload["content_version"]
    progress, status = 0, "running"
    keep_directory = False

    async def cancelled():
        return await service.is_cancel_requested(task_id)

    async def publish(record):
        try:
            await service.bus.publish(task_id, record.status, record.progress, error_code=record.error_code)
        except AppException:
            pass

    async def on_progress(event):
        nonlocal progress, status
        if intermediate:
            if intermediate_progress:
                await intermediate_progress(event)
            return
        progress = max(progress, min(95, event["progress"]))
        if status == "running" and event.get("stage") in {"processing", "asr"}:
            if await database_call(tasks.mark_processing, task_id):
                status = "processing"
        try:
            await service.bus.publish(task_id, status, progress, stage=event.get("stage"))
        except AppException:
            pass

    try:
        if not intermediate:
            await publish(task)
        cached = await database_call(transcripts.find_cache, task.video_id, language, version, asr.model)
        if cached:
            if intermediate:
                return cached
            task = await database_call(tasks.complete_with, task_id, result(cached, True), lambda session: None)
            await publish(task)
            return {"task_id": str(task_id), "status": task.status}
        await cleanup_expired_files(storage, media, tasks)
        with storage.lease():
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            storage.check_capacity(5 * 1024 * 1024)
            directory = storage.create_task_directory(task_id)
            controls = {"is_cancel_requested": cancelled, "on_progress": on_progress, "check_capacity": storage.check_capacity}
            await on_progress({"stage": "subtitles", "progress": 10})
            data = await subtitles.fetch(task.input_payload, directory, **controls)
            source, model = "subtitle", "none"
            if data is None:
                source, model = "asr", asr.model
                await on_progress({"stage": "audio", "progress": 20})
                # A disabled provider reports configuration failure before any audio download.
                if getattr(asr, "available", True) is False:
                    raise AppException("ASR_NOT_CONFIGURED", "无可用字幕且未配置 ASR；可配置云端密钥或显式启用 Mock", 503)
                if settings.APP_ENV != "development" and getattr(asr, "mock", False):
                    raise AppException("ASR_NOT_CONFIGURED", "生产转写不能使用 Mock ASR", 503)
                storage.check_capacity((2 if "allowance" in task.input_payload else 1) * settings.MAX_FILE_SIZE_MB * 1024 * 1024)
                path = await audio.acquire(task.input_payload, directory, **controls)
                if "allowance" in task.input_payload:
                    from app.modules.media.probe import audio_seconds
                    duration = await audio_seconds(path, directory, controls, settings.MAX_FILE_SIZE_MB * 1024 * 1024)
                    await database_call(tasks.record_asr, task_id, duration)
                await on_progress({"stage": "asr", "progress": 90})
                data = await cooperative_asr(asr, path, language, cancelled)
            data = make_transcript(language or data.language, data.segments)
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)

            def store(session):
                item = transcripts.store(session, task.video_id, task_id, source, data, version, model)
                return result(item)

            if intermediate:
                return await database_call(tasks.perform_if_active, task_id,
                    lambda session: transcripts.store(session, task.video_id, task_id, source, data, version, model))
            task = await database_call(tasks.complete_with, task_id, {}, store)
        await publish(task)
    except Exception as exc:
        keep_directory = isinstance(exc, AppException) and exc.code == "PROCESS_CLEANUP_FAILED"
        if intermediate:
            raise
        current = await database_call(tasks.get, task_id)
        if current.status == "completed":
            await publish(current)
            return {"task_id": str(task_id), "status": current.status}
        code = exc.code if isinstance(exc, AppException) else "TRANSCRIPTION_FAILED"
        message = exc.message if isinstance(exc, AppException) else "转写任务失败"
        task = await database_call(tasks.finish, task_id, "cancelled" if code == "TASK_CANCELLED" else "failed", progress,
                                   error_code=code, error_message=message)
        await publish(task)
    finally:
        if not keep_directory:
            storage.remove_task_directory(task_id)  # Never deletes a reused media file outside this directory.
    return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="transcripts.transcribe")
def transcription_task(task_id: str):
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                               socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            storage = LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
            media = MediaRepository(SessionLocal)
            return await execute_transcription(UUID(task_id), TaskService(TaskRepository(SessionLocal), ProgressBus(redis)),
                TranscriptRepository(SessionLocal), media, storage,
                YtDlpSubtitleProvider(ProcessRunner(settings.SUBTITLE_TIMEOUT_SECONDS, 6 * 1024 * 1024)),
                AudioAcquirer(media, storage, ProcessRunner(settings.DOWNLOAD_TIMEOUT_SECONDS, settings.MAX_FILE_SIZE_MB * 1024 * 1024)),
                get_asr_provider())
        finally:
            await redis.aclose()

    return asyncio.run(execute())
