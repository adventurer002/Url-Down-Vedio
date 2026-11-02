from contextlib import asynccontextmanager
from pathlib import Path
from app.infrastructure.process_control import atomic_json
from typing import Protocol
from uuid import UUID

from app.core.exceptions import AppException
from app.infrastructure.storage.retry import wait_for_storage
from app.modules.tasks.service import database_call
from app.modules.tasks.repository import utcnow


class ClipSourceProvider(Protocol):
    def acquire(self, task, directory: Path, **controls): ...


class ClipSources:
    def __init__(self, uploads, media, storage, downloader):
        self.uploads, self.media, self.storage, self.downloader = uploads, media, storage, downloader

    @asynccontextmanager
    async def acquire(self, task, directory, **controls):
        payload = task.input_payload
        if payload.get("source_kind") == "upload":
            identifier = UUID(payload["upload_id"])
            async with wait_for_storage(lambda: self.uploads.files.lock(identifier, shared=True),
                                        is_cancel_requested=controls["is_cancel_requested"]):
                source = await self.uploads.resolve(identifier, task.user_id)
                if source["video_id"] != str(task.video_id) or source["content_sha256"] != payload["content_sha256"]:
                    raise AppException("UPLOAD_SOURCE_MISMATCH", "源文件与分析方案不一致", 409)
                yield self.uploads.files.path(identifier)
            return
        for item in await database_call(self.media.reusable, task.video_id, utcnow()):
            if item.mime_type.startswith("video/"):
                path = self.storage.resolve_file(item.storage_key)
                async with wait_for_storage(lambda: self.storage.file_lock(path),
                                            is_cancel_requested=controls["is_cancel_requested"]):
                    if path.is_file() and path.stat().st_size == item.size_bytes:
                        # Survives worker death, when its flock is gone but an independent FFmpeg group may remain.
                        # Serialize dependency publication with cleanup's single protection snapshot.
                        # The shared file lock is already held; no child may start until the pin is durable.
                        async with wait_for_storage(self.storage.lease,
                                is_cancel_requested=controls["is_cancel_requested"]):
                            atomic_json(directory / ".clip-source.json", {"storage_key": item.storage_key, "video_id": str(task.video_id)})
                        yield path
                        return
        path = await self.downloader.download({**payload, "preset_id": "video_720_mp4"}, directory, **controls)
        yield path
