from app.core.security import validate_url
from app.modules.tasks.service import database_call
from app.modules.video.parsers.base import VideoParser
from app.modules.video.repository import VideoRepository


class VideoResolver:
    def __init__(self, parser: VideoParser, repository: VideoRepository):
        self.parser, self.repository = parser, repository

    async def resolve(self, url: str) -> dict:
        normalized = await validate_url(url)
        existing = await database_call(self.repository.find_by_url, normalized)
        if existing:
            return existing
        video = await self.parser.parse(normalized)
        video_id = await database_call(self.repository.get_or_create, video, normalized)
        return {"video_id": str(video_id), "platform": video.platform, "platform_video_id": video.platform_video_id,
                "title": video.title, "url": normalized}
