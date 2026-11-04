from app.modules.tasks.service import database_call
from app.modules.video.parsers.base import VideoParser
from app.modules.video.repository import VideoRepository
from app.modules.video.parsers.operations import metadata_budget, prepare_source_link


class VideoResolver:
    def __init__(self, parser: VideoParser, repository: VideoRepository, uploads=None):
        self.parser, self.repository, self.uploads = parser, repository, uploads

    async def resolve(self, url: str | None = None, *, upload_id=None, user_id=None) -> dict:
        with metadata_budget():
            return await self._resolve(url, upload_id=upload_id, user_id=user_id)

    async def download_selection(self, payload: dict) -> dict:
        from app.core.exceptions import AppException
        from app.modules.download.presets import source_selection

        with metadata_budget():
            url = await prepare_source_link(payload["url"])
            video = await self.parser.parse(url)
        if (video.platform, video.platform_video_id) != (payload["platform"], payload["platform_video_id"]):
            raise AppException("VIDEO_ID_CHANGED", "下载来源的视频身份发生变化", 422)
        return source_selection(video)

    async def _resolve(self, url, *, upload_id, user_id):
        if upload_id is not None:
            from app.modules.uploads.service import upload_service
            return await (self.uploads or upload_service(self.repository.sessions)).resolve(upload_id, user_id)
        normalized = await prepare_source_link(url)
        existing = await database_call(self.repository.find_by_url, normalized)
        if existing:
            return existing
        video = await self.parser.parse(normalized)
        video_id = await database_call(self.repository.get_or_create, video, normalized)
        return {"video_id": str(video_id), "platform": video.platform, "platform_video_id": video.platform_video_id,
                "title": video.title, "url": normalized}
