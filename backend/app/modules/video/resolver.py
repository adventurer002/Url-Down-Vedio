from app.core.security import validate_url
from app.modules.tasks.service import database_call
from app.modules.video.parsers.base import VideoParser
from app.modules.video.repository import VideoRepository


class VideoResolver:
    def __init__(self, parser: VideoParser, repository: VideoRepository, uploads=None):
        self.parser, self.repository, self.uploads = parser, repository, uploads

    async def resolve(self, url: str | None = None, *, upload_id=None, user_id=None) -> dict:
        if upload_id is not None:
            from app.modules.uploads.service import upload_service
            return await (self.uploads or upload_service(self.repository.sessions)).resolve(upload_id, user_id)
        normalized = await validate_url(url)
        existing = await database_call(self.repository.find_by_url, normalized)
        if existing:
            return existing
        video = await self.parser.parse(normalized)
        video_id = await database_call(self.repository.get_or_create, video, normalized)
        return {"video_id": str(video_id), "platform": video.platform, "platform_video_id": video.platform_video_id,
                "title": video.title, "url": normalized}
