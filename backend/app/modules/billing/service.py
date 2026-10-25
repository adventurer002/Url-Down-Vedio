import asyncio
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.billing.policy import seconds
from app.modules.tasks.service import database_call


class AllowanceDispatcher:
    def enqueue(self, task_id):
        from app.infrastructure.queue.celery_app import celery_app
        celery_app.send_task("billing.execute", args=[str(task_id)], task_id=str(task_id))


class BillingService:
    def __init__(self, repository, resolver, videos, tasks):
        self.repository, self.resolver, self.videos, self.tasks = repository, resolver, videos, tasks

    async def quote(self, user_id, url, action, preset_id, language):
        payload = await self.resolver.resolve(url)
        metadata = await database_call(self.videos.history_metadata, [UUID(payload["video_id"])])
        duration = seconds(metadata[UUID(payload["video_id"])]["duration"])
        payload.update(duration_seconds=duration, preset_id=preset_id if action == "download" else "audio_mp3",
            language=None if action.startswith("ai_") else language,
            output_language=language or "zh", content_version=settings.TRANSCRIPT_CONTENT_VERSION)
        if action.startswith("ai_"):
            from app.modules.ai.providers import get_llm_provider
            owned = await database_call(self.tasks.repository.own_ai_result, user_id, UUID(payload["video_id"]),
                action, language or "zh", get_llm_provider().model, settings.SUMMARY_PROMPT_VERSION,
                settings.MINDMAP_PROMPT_VERSION + ":" + settings.MINDMAP_SCHEMA_VERSION)
            if owned:
                payload["reuse_result"] = owned
        return await database_call(self.repository.save_quote, user_id, action, payload)

    async def confirm(self, user_id, quote_id):
        action, payload, _ = await database_call(self.repository.quote, user_id, quote_id)
        from app.modules.auth.dependencies import task_user_id
        marker = task_user_id.set(user_id)
        try:
            return await self.tasks.create(action, UUID(payload["video_id"]), payload)
        finally:
            task_user_id.reset(marker)


async def resume_queued(tasks, user_id=None):
    dispatched = 0
    for task in await database_call(tasks.queued, user_id):
        if "allowance" in task.input_payload:
            try:
                async with asyncio.timeout(5):
                    await asyncio.to_thread(AllowanceDispatcher().enqueue, task.id)
                await database_call(tasks.mark_dispatched, task.id)
                dispatched += 1
            except Exception:
                break  # Durable queued rows remain retryable; no infinite broker retries.
    return dispatched
