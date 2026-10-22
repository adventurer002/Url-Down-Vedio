from dataclasses import asdict
from uuid import UUID

from app.core.config import settings
from app.modules.tasks.service import database_call


class AIService:
    def __init__(self, resolver, tasks, results):
        self.resolver, self.tasks, self.results = resolver, tasks, results

    async def create(self, url, language, result_type):
        payload = await self.resolver.resolve(url)
        return await self.tasks.create("ai_" + result_type, UUID(payload["video_id"]),
            {**payload, "output_language": language.lower(), "language": None,
             "content_version": settings.TRANSCRIPT_CONTENT_VERSION, "preset_id": "audio_mp3"})

    async def get(self, result_id):
        return asdict(await database_call(self.results.get, result_id))
