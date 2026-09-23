"""AI celery task: single dispatcher, retries only retryable LLM failures."""

import asyncio
import uuid
from pathlib import Path

import redis as sync_redis
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.core.errors import AppError
from backend.app.db.models import ProcessingTask
from backend.app.providers.asr import ASRProvider, FasterWhisperASR
from backend.app.providers.downloader import DownloadCancelled
from backend.app.providers.ffmpeg import FFmpegCancelled, FFmpegError
from backend.app.providers.llm import LLMError
from backend.app.providers.openai_llm import OpenAICompatibleLLM
from backend.app.providers.storage import StorageProvider
from backend.app.services import ai_service as ai
from backend.app.services.progress import cancel_requested
from backend.app.worker.celery_app import celery_app
from backend.app.worker.tasks import _session_factory, build_storage


def build_llm() -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        timeout=120,
    )


async def _ai_impl(
    task_id: uuid.UUID,
    celery_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    llm: OpenAICompatibleLLM | None = None,
    storage: StorageProvider | None = None,
    asr: ASRProvider | None = None,
) -> None:
    maker = session_factory or _session_factory()
    store = storage or build_storage()
    redis_client = sync_redis.Redis.from_url(settings.redis_url)

    async with maker() as session:
        task = await session.get(ProcessingTask, task_id)
        if task is None or task.status not in ("queued", "running"):
            return
        task.celery_task_id = celery_id
        task.status = "running"
        task.started_at = ai._now()
        await session.commit()

        def should_cancel() -> bool:
            return cancel_requested(redis_client, task.id)

        work_dir = ai.work_dir_for(Path(settings.worker_tmp_dir), task.id)
        try:
            if task.task_type == "audio_extract":
                media = await ai.run_audio_extract(
                    session, task, store, work_dir, should_cancel
                )
                task.output_id = media.id
            elif task.task_type == "transcribe":
                provider = asr or FasterWhisperASR(settings.whisper_model)
                await ai.run_transcribe(
                    session, task, store, provider, work_dir, should_cancel
                )
                task.output_id = await ai.find_existing_output(
                    session, task.video_id, "transcribe"
                )
            elif task.task_type == "summarize":
                provider_llm = llm or build_llm()
                summary = await ai.run_summarize(session, task, provider_llm)
                task.output_id = summary.id
            elif task.task_type == "mindmap":
                provider_llm = llm or build_llm()
                mindmap = await ai.run_mindmap(session, task, provider_llm)
                task.output_id = mindmap.id
            else:
                raise AppError("validation_error", f"未知任务类型: {task.task_type}")
            task.status = "completed"
            task.finished_at = ai._now()
            await session.commit()
        except (DownloadCancelled, FFmpegCancelled):
            task.status = "cancelled"
            task.finished_at = ai._now()
            await session.commit()
        except AppError as e:
            code = "download_failed"
            if isinstance(e.details, dict):
                code = str(e.details.get("error_code") or code)
            if code == "llm_error":
                await session.rollback()
                raise LLMError(e.message, retryable=True)
            await ai.mark_failed(session, task.id, code, e.message)
        except LLMError:
            await session.rollback()
            raise
        except FFmpegError as e:
            await ai.mark_failed(session, task.id, "ffmpeg_error", str(e))
        except Exception as e:  # noqa: BLE001 - worker must record, never leak
            await ai.mark_failed(session, task.id, "download_failed", str(e))
        finally:
            ai.cleanup_dir(work_dir)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.ai_tasks.ai_process",
    soft_time_limit=1500,
    time_limit=1800,
    queue="ai",
    max_retries=2,
    autoretry_for=(LLMError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 2},
)
def ai_process(task_id_str: str) -> str:
    from celery import current_task

    task_id = uuid.UUID(task_id_str)
    try:
        asyncio.run(_ai_impl(task_id, current_task.request.id or ""))
    except MaxRetriesExceededError:
        asyncio.run(_fail_after_retries(task_id))
        return "failed"
    except LLMError as e:
        if not e.retryable:
            return "failed"
        raise
    return "ok"


async def _fail_after_retries(task_id: uuid.UUID) -> None:
    maker = _session_factory()
    async with maker() as session:
        await ai.mark_failed(session, task_id, "llm_error", "LLM 重试耗尽")
