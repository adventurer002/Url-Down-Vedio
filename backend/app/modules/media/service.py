from datetime import timezone
import re
import unicodedata
from uuid import UUID

from app.core.exceptions import AppException
from app.infrastructure.storage.base import StorageAdapter
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import database_call


def safe_filename(title: str, extension: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    title = "".join(char for char in title if not unicodedata.category(char).startswith("C"))
    title = re.sub(r'[/\\:"<>|?*]', "_", title).strip(" .")[:100]
    return f"{title or 'video'}.{extension}"


class MediaService:
    def __init__(self, repository: MediaRepository, storage: StorageAdapter):
        self.repository = repository
        self.storage = storage

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
