import asyncio
from uuid import UUID
from types import SimpleNamespace

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.billing.policy import seconds
from app.modules.tasks.service import database_call
from app.modules.video.parsers.operations import metadata_budget


class AllowanceDispatcher:
    def enqueue(self, task_id):
        from app.infrastructure.queue.celery_app import celery_app
        celery_app.send_task("billing.execute", args=[str(task_id)], task_id=str(task_id))


class BillingService:
    def __init__(self, repository, resolver, videos, tasks):
        self.repository, self.resolver, self.videos, self.tasks = repository, resolver, videos, tasks

    async def quote(self, user_id, url, action, preset_id, language, *, upload_id=None, transcript_id=None):
        from app.modules.video.sources import resolve_input
        with metadata_budget():
            if upload_id is not None and action not in {"transcription", "ai_summary", "ai_mindmap"}:
                raise AppException("UPLOAD_ACTION_UNSUPPORTED", "上传文件支持转写、总结和思维导图；问答通过已有文稿创建", 422)
            if transcript_id is not None:
                if url is not None or upload_id is not None or action not in {"ai_summary", "ai_mindmap"}:
                    raise AppException("TRANSCRIPT_ACTION_UNSUPPORTED", "已有文稿仅支持总结或思维导图，且不能同时提供其他来源", 422)
                payload = await self._transcript_source(transcript_id, user_id)
                duration = 0
            else:
                payload = await resolve_input(self.resolver, url, upload_id, user_id)
                metadata = await database_call(self.videos.history_metadata, [UUID(payload["video_id"])])
                duration = payload.get("duration_seconds") or seconds(metadata[UUID(payload["video_id"])]["duration"])
            payload.update(duration_seconds=duration, preset_id=preset_id if action == "download" else "audio_mp3",
                language=None if action.startswith("ai_") else language,
                output_language=language or "zh", content_version=payload.get("content_version", settings.TRANSCRIPT_CONTENT_VERSION))
            if action == "download" and preset_id == "video_source_mp4":
                payload["source_selection"] = await self.resolver.download_selection(payload)
                payload["duration_seconds"] = seconds(payload["source_selection"]["duration"])
        if action.startswith("ai_"):
            from app.modules.ai.providers import get_llm_provider
            if transcript_id is not None:
                owned = await self._owned_transcript_result(transcript_id, user_id, action, language or "zh", get_llm_provider().model)
            else:
                owned = await database_call(self.tasks.repository.own_ai_result, user_id, UUID(payload["video_id"]),
                    action, language or "zh", get_llm_provider().model, settings.SUMMARY_PROMPT_VERSION,
                    settings.MINDMAP_PROMPT_VERSION + ":" + settings.MINDMAP_SCHEMA_VERSION)
            if owned:
                payload["reuse_result"] = owned
        return await database_call(self.repository.save_quote, user_id, action, payload)

    async def confirm(self, user_id, quote_id):
        action, payload, existing_task_id = await database_call(self.repository.quote, user_id, quote_id)
        if action in {"ai_clip_plan", "clips_export"} and existing_task_id is None:
            from app.modules.clips.service import clip_service
            await clip_service(self.videos.sessions, tasks=self.tasks).revalidate(user_id, action, payload)
        if payload.get("source_kind") == "transcript" and existing_task_id is None:
            current = await self._transcript_source(UUID(payload["transcript_id"]), user_id)
            if current["video_id"] != payload["video_id"] or current["transcript_content_hash"] != payload["transcript_content_hash"]:
                raise AppException("TRANSCRIPT_SOURCE_MISMATCH", "任务文稿来源发生变化", 409)
        if payload.get("upload_id") and existing_task_id is None:
            await self.resolver.resolve(upload_id=UUID(payload["upload_id"]), user_id=user_id)
        from app.modules.auth.dependencies import task_user_id
        marker = task_user_id.set(user_id)
        try:
            return await self.tasks.create(action, UUID(payload["video_id"]), payload)
        finally:
            task_user_id.reset(marker)

    async def _transcript_source(self, transcript_id, user_id):
        from app.modules.transcript.repository import TranscriptRepository
        from app.modules.transcript.service import TranscriptService
        return await TranscriptService(None, self.tasks, TranscriptRepository(self.videos.sessions)).ai_source(transcript_id, user_id)

    async def _owned_transcript_result(self, transcript_id, user_id, action, language, model):
        from app.modules.ai.repository import AIRepository
        from app.modules.transcript.repository import TranscriptRepository
        results = AIRepository(self.videos.sessions)
        transcript = await database_call(TranscriptRepository(self.videos.sessions).get, transcript_id)
        scope = await database_call(results.scope_for, transcript, exact=True)
        kind = action.removeprefix("ai_")
        version = settings.SUMMARY_PROMPT_VERSION if kind == "summary" else settings.MINDMAP_PROMPT_VERSION + ":" + settings.MINDMAP_SCHEMA_VERSION
        cached = await database_call(results.find_cache, transcript.content_hash, kind, model, version, language, scope)
        if cached is None or not await self.tasks.can_read_results("result_id", [cached.id], SimpleNamespace(id=user_id)):
            return None
        value = {"result_id": str(cached.id), "result_type": kind, "transcript_id": str(transcript_id), "cache_hit": True}
        if kind == "mindmap":
            summary = await database_call(results.find_cache, transcript.content_hash, "summary", model, settings.SUMMARY_PROMPT_VERSION, language, scope)
            if summary and await self.tasks.can_read_results("result_id", [summary.id], SimpleNamespace(id=user_id)):
                value["summary_result_id"] = str(summary.id)
        return value


async def resume_queued(tasks, user_id=None):
    dispatched = 0
    for task in await database_call(tasks.queued, user_id):
        if "allowance" in task.input_payload or task.task_type == "media_probe":
            try:
                async with asyncio.timeout(5):
                    if task.task_type == "media_probe":
                        from app.modules.uploads.worker import UploadDispatcher
                        await asyncio.to_thread(UploadDispatcher().enqueue, task.id)
                    else:
                        await asyncio.to_thread(AllowanceDispatcher().enqueue, task.id)
                await database_call(tasks.mark_dispatched, task.id)
                dispatched += 1
            except Exception:
                break  # Durable queued rows remain retryable; no infinite broker retries.
    return dispatched
