import asyncio
from types import SimpleNamespace
from uuid import UUID

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.modules.ai.providers import get_llm_provider
from app.modules.ai.providers.bounded import BoundedLLMProvider
from app.modules.knowledge.questions import answer_question
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.transcript.repository import TranscriptRepository


async def execute_question(task_id, service, repository, transcripts, provider):
    tasks = service.repository
    if not await database_call(tasks.start, task_id):
        return {"task_id": str(task_id), "status": (await database_call(tasks.get, task_id)).status}
    task = await database_call(tasks.get, task_id)
    payload = task.input_payload
    try:
        async with asyncio.timeout(settings.AI_TASK_TIMEOUT_SECONDS):
            from app.modules.transcript.service import TranscriptService
            data = await TranscriptService(None, service, transcripts).get(UUID(payload["transcript_id"]), SimpleNamespace(id=task.user_id))
            transcript = SimpleNamespace(**data)
            await database_call(tasks.mark_processing, task_id)
            try:
                await service.bus.publish(task_id, "processing", 20, stage="question")
            except AppException:
                pass
            cached = await database_call(repository.cached_answer, task.user_id, payload["question_key"])
            if cached:
                task = await database_call(tasks.complete_with, task_id,
                    {"answer_id": str(cached["id"]), "transcript_id": str(transcript.id), "reused": True}, lambda session: None)
            else:
                if provider.name == "openai_compatible":
                    provider.model = payload["allowance"]["llm_model"]
                provider = BoundedLLMProvider(provider, {"max_calls": 1, "total_output_tokens": 4096, "output_tokens_per_call": 4096})
                answer, citations, usage = await answer_question(provider, transcript, payload["question"],
                    payload["output_language"], lambda: service.is_cancel_requested(task_id))
                def write(session):
                    identifier = repository.store_answer(session, task.user_id, task_id,
                        {**payload, "transcript_uuid": transcript.id}, answer, citations, usage)
                    return {"answer_id": str(identifier), "transcript_id": str(transcript.id)}
                task = await database_call(tasks.complete_with, task_id, {}, write)
    except Exception as error:
        code = error.code if isinstance(error, AppException) else "AI_TIMEOUT" if isinstance(error, TimeoutError) else "QUESTION_FAILED"
        message = error.message if isinstance(error, AppException) else "视频问答暂未完成，本次不扣额度"
        task = await database_call(tasks.finish, task_id, "cancelled" if code == "TASK_CANCELLED" else "failed", 0,
            error_code=code, error_message=message)
    try:
        await service.bus.publish(task_id, task.status, task.progress, error_code=task.error_code)
    except AppException:
        pass
    return {"task_id": str(task_id), "status": task.status}


@celery_app.task(name="knowledge.question")
def question_task(task_id):
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
            socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            return await execute_question(UUID(task_id), TaskService(TaskRepository(SessionLocal), ProgressBus(redis)),
                KnowledgeRepository(SessionLocal), TranscriptRepository(SessionLocal), get_llm_provider())
        finally:
            await redis.aclose()
    return asyncio.run(execute())
