import asyncio
from datetime import timezone
import re
import unicodedata
from uuid import UUID

from app.core.exceptions import AppException
from app.infrastructure.storage.base import StorageAdapter
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import database_call
from app.modules.tasks.service import TaskService
from app.modules.video.resolver import VideoResolver


def safe_filename(title: str, extension: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    title = "".join(char for char in title if not unicodedata.category(char).startswith("C"))
    title = re.sub(r'[/\\:"<>|?*]', "_", title).strip(" .")[:100]
    return f"{title or 'video'}.{extension}"


class MediaService:
    def __init__(self, repository: MediaRepository, storage: StorageAdapter):
        self.repository = repository
        self.storage = storage

    async def available_ids(self, file_ids: list[UUID]) -> set[str]:
        records = await database_call(self.repository.available, file_ids, utcnow())
        def check():
            available = set()
            for item in records:
                try:
                    path = self.storage.resolve_file(item.storage_key)
                    if path.is_file() and path.stat().st_size == item.size_bytes:
                        available.add(str(item.id))
                except (AppException, OSError):
                    continue
            return available
        return await asyncio.to_thread(check)

    async def download(self, file_id: UUID):
        record = await database_call(self.repository.get, file_id)
        expiry = record.expires_at.replace(tzinfo=timezone.utc) if record.expires_at.tzinfo is None else record.expires_at
        if expiry <= utcnow():
            raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
        path = self.storage.resolve_file(record.storage_key)
        if not path.is_file() or path.stat().st_size != record.size_bytes:
            raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
        extension = "mp3" if record.mime_type == "audio/mpeg" else "mp4"
        return record, path, safe_filename(record.filename.rsplit(".", 1)[0], extension)


class AudioExtractionService:
    def __init__(self, resolver: VideoResolver, tasks: TaskService):
        self.resolver, self.tasks = resolver, tasks

    async def create(self, url: str):
        payload = await self.resolver.resolve(url)
        return await self.tasks.create("audio_extraction", UUID(payload["video_id"]), {**payload, "preset_id": "audio_mp3"})
