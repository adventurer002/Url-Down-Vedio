from app.core.exceptions import AppException
from app.core.security import validate_url
from app.infrastructure.storage.base import StorageAdapter
from app.modules.download.presets import PRESETS
from app.modules.tasks.service import TaskService, database_call
from app.modules.video.parsers.base import VideoParser
from app.modules.video.repository import VideoRepository
from app.modules.video.service import aggregate_download_options


class DownloadService:
    def __init__(self, parser: VideoParser, videos: VideoRepository, tasks: TaskService, storage: StorageAdapter):
        self.parser, self.videos, self.tasks, self.storage = parser, videos, tasks, storage

    async def create(self, url: str, preset_id: str):
        if preset_id not in PRESETS:
            raise AppException("INVALID_PRESET", "下载档位无效", 422)
        normalized = await validate_url(url)
        self.storage.check_capacity()
        video = await self.parser.parse(normalized)
        if preset_id not in {item.id for item in aggregate_download_options(video)}:
            raise AppException("PRESET_UNAVAILABLE", "当前视频不提供所选下载档位", 422)
        video_id = await database_call(self.videos.get_or_create, video, normalized)
        return await self.tasks.create("download", video_id=video_id, input_payload={
            "url": normalized, "preset_id": preset_id, "title": video.title,
            "platform": video.platform, "platform_video_id": video.platform_video_id,
        })
