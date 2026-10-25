import asyncio
from uuid import UUID

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.storage import LocalStorageAdapter
from app.modules.ai.mindmap import MindMapGenerator
from app.modules.ai.providers import get_llm_provider
from app.modules.ai.repository import AIRepository
from app.modules.ai.schemas import VideoSummarySchema
from app.modules.ai.summarizer import Summarizer
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.transcript.service import TranscriptionPipeline


class AIDispatcher:
    def enqueue(self, task_id):
        celery_app.send_task("ai.generate", args=[str(task_id)], task_id=str(task_id))


def result(item, cache_hit=False):
    return {"result_id": str(item.id), "result_type": item.result_type, "cache_hit": cache_hit}


async def execute_ai(task_id, service, results, pipeline, provider):
    tasks = service.repository
    if not await database_call(tasks.start, task_id):
        return {"task_id": str(task_id), "status": (await database_call(tasks.get, task_id)).status}
    task = await database_call(tasks.get, task_id)
    progress = 0

    async def publish(record, stage=None):
        try:
            await service.bus.publish(task_id, record.status, record.progress, stage=stage, error_code=record.error_code)
        except AppException:
            pass

    async def cancelled():
        return await service.is_cancel_requested(task_id)

    async def advance(value, stage="ai"):
        nonlocal progress
        progress = max(progress, min(95, value))
        try:
            await service.bus.publish(task_id, "processing", progress, stage=stage)
        except AppException:
            pass

    try:
        await publish(task)
        await database_call(tasks.mark_processing, task_id)
        async with asyncio.timeout(settings.AI_TASK_TIMEOUT_SECONDS):
            transcript = await pipeline.acquire(task_id, lambda event: advance(int(event["progress"] * .4), event.get("stage")))
            if "allowance" in task.input_payload:
                from app.modules.ai.providers.bounded import BoundedLLMProvider
                limits = task.input_payload["allowance"]["ai_limits"]
                if len(transcript.content_text) > limits["text_chars"]:
                    raise AppException("TRANSCRIPT_TOO_LONG", "首版单次文稿最多 60000 字符", 413)
                if provider.name == "openai_compatible":
                    provider.model = task.input_payload["allowance"]["llm_model"]
                provider = BoundedLLMProvider(provider, limits)
            language = task.input_payload["output_language"]
            kind = task.task_type.removeprefix("ai_")
            summary_version = settings.SUMMARY_PROMPT_VERSION
            version = summary_version if kind == "summary" else settings.MINDMAP_PROMPT_VERSION + ":" + settings.MINDMAP_SCHEMA_VERSION
            if "allowance" in task.input_payload:
                summary_version = task.input_payload["allowance"]["summary_version"]
                version = summary_version if kind == "summary" else task.input_payload["allowance"]["mindmap_version"]

            def delivered(item, cache_hit=False, summary_id=None):
                value = {**result(item, cache_hit), "transcript_id": str(transcript.id)}
                if summary_id:
                    value["summary_result_id"] = str(summary_id)
                return value

            cached = await database_call(results.find_cache, transcript.content_hash, kind, provider.model, version, language)
            if cached:
                summary_cached = await database_call(results.find_cache, transcript.content_hash, "summary", provider.model, summary_version, language)
                task = await database_call(tasks.complete_with, task_id, delivered(cached, True,
                    summary_cached.id if kind == "mindmap" and summary_cached else None), lambda session: None)
            else:
                summary_cache = await database_call(results.find_cache, transcript.content_hash, "summary", provider.model, summary_version, language)
                if summary_cache:
                    summary = VideoSummarySchema.model_validate(summary_cache.content_json)
                    summary_id = summary_cache.id
                else:
                    generated = await Summarizer(provider).summarize(transcript, language, cancelled=cancelled,
                        on_progress=lambda value: advance(40 + int(value * .45), "summary"))
                    summary = generated.content
                    def store_summary(session):
                        return results.store(session, task_id, transcript, "summary", provider.name, provider.model,
                            summary_version, language, summary.model_dump(), generated.usage)
                    if kind == "summary":
                        task = await database_call(tasks.complete_with, task_id, {}, lambda session: delivered(store_summary(session)))
                    else:
                        summary_record = await database_call(tasks.perform_if_active, task_id, store_summary)
                        summary_id = summary_record.id
                if kind == "summary" and summary_cache:
                    task = await database_call(tasks.complete_with, task_id, delivered(summary_cache, True), lambda session: None)
                elif kind == "mindmap":
                    await advance(90, "mindmap")
                    mapped = await MindMapGenerator(provider).generate(summary, language, cancelled=cancelled)
                    task = await database_call(tasks.complete_with, task_id, {}, lambda session: delivered(results.store(
                        session, task_id, transcript, "mindmap", provider.name, provider.model, version, language,
                        mapped.content.model_dump(), mapped.usage, mapped.markdown), summary_id=summary_id))
    except Exception as exc:
        code = exc.code if isinstance(exc, AppException) else "AI_TIMEOUT" if isinstance(exc, TimeoutError) else "AI_GENERATION_FAILED"
        message = exc.message if isinstance(exc, AppException) else "AI 任务超时" if isinstance(exc, TimeoutError) else "AI 生成失败"
        task = await database_call(tasks.finish, task_id, "cancelled" if code == "TASK_CANCELLED" else "failed", progress,
                                   error_code=code, error_message=message)
    await publish(task)
    return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="ai.generate")
def ai_task(task_id: str):
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                               socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            service = TaskService(TaskRepository(SessionLocal), ProgressBus(redis))
            storage = LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
            return await execute_ai(UUID(task_id), service, AIRepository(SessionLocal),
                TranscriptionPipeline(service, SessionLocal, storage), get_llm_provider())
        finally:
            await redis.aclose()
    return asyncio.run(execute())
