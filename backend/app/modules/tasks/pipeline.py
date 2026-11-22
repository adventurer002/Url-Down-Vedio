"""Execute one durable phase per Celery delivery on a single shared host."""
import asyncio
import logging
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.process_control import task_process_scope
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.storage import LocalStorageAdapter
from app.infrastructure.timeouts import operation_timeout
from app.modules.tasks.checkpoints import Checkpoints, active_checkpoints
from app.modules.tasks.dispatch import enqueue
from app.modules.tasks.execution import execution_lock
from app.modules.tasks.models import TERMINAL_STATUSES
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.service import TaskService, database_call

logger = logging.getLogger(__name__)


async def publish(service, task, stage=None, progress=None):
    try:
        await service.bus.publish(task.id, task.status, task.progress if progress is None else progress,
                                  stage=stage, error_code=task.error_code)
    except AppException:
        pass


async def execute_stage(service, storage, task, execution_id):
    from app.modules.transcript.repository import TranscriptRepository
    from app.modules.transcript.service import TranscriptionPipeline

    tasks, task_id = service.repository, task.id
    pipeline = TranscriptionPipeline(service, tasks.sessions, storage)
    if task.phase in {"download", "render"}:
        from app.modules.download.adapter import LocalRenderAdapter, YtDlpDownloadAdapter
        from app.modules.download.delivery import DeliveryPreparer
        from app.modules.download.worker import execute_download
        from app.modules.media.repository import MediaRepository

        async def handoff():
            await database_call(tasks.handoff_phase, task_id, execution_id, "download", "render", 85)

        global_limit = settings.MAX_FILE_SIZE_MB * 1024 * 1024
        limit = min(global_limit, task.input_payload.get("allowance", {}).get("limits", {}).get("file_bytes", global_limit))
        render = task.phase == "render"
        adapter = LocalRenderAdapter(ProcessRunner(settings.DOWNLOAD_RENDER_TIMEOUT_SECONDS, 2 * limit + 4096)) if render else (
            YtDlpDownloadAdapter(ProcessRunner(settings.DOWNLOAD_TIMEOUT_SECONDS, limit + 4096)))
        await execute_download(task_id, service, MediaRepository(tasks.sessions), storage, adapter,
                               preparer=None if render else DeliveryPreparer(), claimed=True,
                               defer_render=None if render else handoff, render=render)
        return

    if task.phase == "llm":
        from app.modules.ai.providers import get_llm_provider
        from app.modules.ai.repository import AIRepository
        from app.modules.ai.worker import execute_ai

        async def acquire(identifier, progress):
            if task.prepared_transcript_id is None:
                return await pipeline.acquire(identifier, progress)
            transcript = await database_call(TranscriptRepository(tasks.sessions).get, task.prepared_transcript_id)
            if transcript.video_id != task.video_id:
                raise AppException("TRANSCRIPT_SOURCE_MISMATCH", "任务文稿来源发生变化", 409)
            return transcript

        await execute_ai(task_id, service, AIRepository(tasks.sessions), SimpleNamespace(acquire=acquire),
                         None if settings.MODEL_CONFIG_ENABLED else get_llm_provider(), claimed=True)
        return

    from app.modules.transcript.worker import PreparedAudio, result
    await database_call(tasks.mark_processing, task_id)
    processing = await database_call(tasks.get, task_id)

    async def progress(event):
        value = event["progress"]
        if task.task_type != "transcription":
            value = int(value * .4)
        await publish(service, processing, event["stage"], max(task.progress, value))

    transcript = await pipeline.acquire(task_id, progress, phase=task.phase)
    if isinstance(transcript, PreparedAudio):
        await database_call(tasks.handoff_phase, task_id, execution_id, "prepare", "asr",
                            30 if task.task_type == "transcription" else 20)
    elif task.task_type == "transcription":
        reused = transcript.task_id != task_id
        await database_call(tasks.complete_with, task_id, result(transcript, cache_hit=reused), lambda session: None)
    else:
        await database_call(tasks.handoff_phase, task_id, execution_id, task.phase, "llm", 40, transcript.id)


async def run_phase(task_id, phase, service, storage, *, body=execute_stage, dispatch=enqueue):
    tasks = service.repository
    execution_id = str(uuid4())
    try:
        with execution_lock(storage.root, task_id):
            download_cache_ready = False
            if phase == "download" and settings.ALLOWANCES_ENABLED:
                pending = await database_call(tasks.get, task_id)
                if pending.status == "queued" and pending.phase == phase and pending.user_id:
                    from app.modules.download.worker import owned_cached_download
                    from app.modules.media.repository import MediaRepository

                    download_cache_ready = bool(await owned_cached_download(
                        pending, service, MediaRepository(tasks.sessions), storage))
            if not await database_call(tasks.claim_phase, task_id, phase, execution_id,
                                       download_cache_ready=download_cache_ready):
                return (await database_call(tasks.get, task_id)).status
            current = await database_call(tasks.get, task_id)
            if current.last_dispatched_at is not None:
                dispatched = current.last_dispatched_at
                if dispatched.tzinfo is None:
                    dispatched = dispatched.replace(tzinfo=timezone.utc)
                logger.info("performance phase=%s task_id=%s dispatch_to_claim_ms=%d",
                            phase, task_id, max(0, round((utcnow() - dispatched).total_seconds() * 1000)))
            token = active_checkpoints.set(Checkpoints(tasks.sessions, current))
            try:
                await publish(service, current, phase)
                started = current.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                limit = settings.AI_TASK_TIMEOUT_SECONDS
                if current.task_type == "download":
                    limit = settings.DOWNLOAD_TIMEOUT_SECONDS + settings.DOWNLOAD_RENDER_TIMEOUT_SECONDS + 30
                remaining = limit - (utcnow() - started).total_seconds()
                code = "DOWNLOAD_TIMEOUT" if current.task_type == "download" else "AI_TIMEOUT" if current.task_type.startswith("ai_") else "TRANSCRIPTION_TIMEOUT"
                if remaining <= 0:
                    raise AppException(code, "任务超过总执行时限", 504)
                async with operation_timeout(phase, remaining, code, "任务超过总执行时限", task_id):
                    with task_process_scope(storage.root / "tmp" / str(task_id), execution_id, current.attempts):
                        await body(service, storage, current, execution_id)
            except Exception as error:
                if not isinstance(error, AppException):
                    logger.error("阶段执行失败 task_id=%s phase=%s error_type=%s", task_id, phase, type(error).__name__)
                code = error.code if isinstance(error, AppException) else "TASK_PHASE_FAILED"
                message = error.message if isinstance(error, AppException) else "任务阶段执行失败"
                await database_call(tasks.finish, task_id, "failed", current.progress,
                                    error_code=code, error_message=message, expected_execution_id=execution_id)
            finally:
                active_checkpoints.reset(token)
            current = await database_call(tasks.get, task_id)
            if current.status in TERMINAL_STATUSES and current.task_type != "download":
                try:
                    storage.remove_task_directory(task_id)
                except AppException as error:
                    if error.code != "PROCESS_CLEANUP_FAILED":
                        raise
            await publish(service, current, current.phase if current.status == "processing" else None)
    except AppException as error:
        if error.code != "TASK_EXECUTION_BUSY":
            raise
        return (await database_call(tasks.get, task_id)).status

    if current.status == "processing" and current.worker_pid is None:
        try:
            await asyncio.wait_for(asyncio.to_thread(dispatch, tasks, current, None), timeout=5)
        except Exception:
            logger.warning("阶段派发待恢复 task_id=%s phase=%s", task_id, current.phase)
    elif current.status in TERMINAL_STATUSES and current.user_id:
        from app.modules.billing.service import resume_queued
        await resume_queued(tasks, current.user_id)
    return current.status


@celery_app.task(name="tasks.execute_phase", ignore_result=True)
def phase_task(task_id, phase):
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                               socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            storage = LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
            return await run_phase(UUID(task_id), phase, TaskService(TaskRepository(SessionLocal), ProgressBus(redis)), storage)
        finally:
            await redis.aclose()
    return asyncio.run(execute())
