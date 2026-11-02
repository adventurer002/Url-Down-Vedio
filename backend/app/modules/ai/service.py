from dataclasses import asdict
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.tasks.service import database_call


class AIService:
    def __init__(self, resolver, tasks, results):
        self.resolver, self.tasks, self.results = resolver, tasks, results

    async def create(self, url, language, result_type, *, upload_id=None, transcript_id=None, user_id=None):
        from app.modules.video.sources import resolve_input
        if transcript_id is not None:
            if url is not None or upload_id is not None:
                raise AppException("TRANSCRIPT_ACTION_UNSUPPORTED", "文稿任务不能同时提供其他来源", 422)
            from app.modules.transcript.service import TranscriptService
            from app.modules.transcript.repository import TranscriptRepository
            payload = await TranscriptService(None, self.tasks, TranscriptRepository(self.results.sessions)).ai_source(transcript_id, user_id)
        else:
            payload = await resolve_input(self.resolver, url, upload_id, user_id)
        return await self.tasks.create("ai_" + result_type, UUID(payload["video_id"]),
            {**payload, "output_language": language.lower(), "language": None,
             "content_version": payload.get("content_version", settings.TRANSCRIPT_CONTENT_VERSION), "preset_id": "audio_mp3"})

    async def get(self, result_id, user=None):
        result = await database_call(self.results.get, result_id)
        await self.tasks.authorize_artifact("result_id", result_id, result.task_id, user)
        return asdict(result)
