import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import unicodedata
from uuid import UUID, uuid4

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.storage.local import LocalStorageAdapter
from app.modules.tasks.service import TaskService, database_call
from app.modules.uploads.repository import UploadRepository
from app.modules.uploads.storage import UploadStorage

EXTENSIONS = {"mp4", "mov", "mkv", "webm", "mp3", "m4a"}


def checked_filename(filename):
    filename = unicodedata.normalize("NFKC", filename).replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(c for c in filename if not unicodedata.category(c).startswith("C")).strip(" .")
    if not filename or len(filename) > 255 or Path(filename).suffix.lower().lstrip(".") not in EXTENSIONS:
        raise AppException("UPLOAD_FORMAT_UNSUPPORTED", "支持 MP4、MOV、MKV、WebM 视频和 MP3、M4A 音频", 422)
    return filename, Path(filename).suffix.lower().lstrip(".")


@dataclass
class UploadService:
    repository: UploadRepository
    storage: LocalStorageAdapter
    tasks: TaskService | None = None

    @property
    def files(self):
        return UploadStorage(self.storage)

    async def create(self, user_id, filename, size):
        filename, extension = checked_filename(filename)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise AppException("UPLOAD_SIZE_INVALID", "文件不能为空，且必须提供实际字节数", 422)
        identifier = uuid4()
        item = await database_call(self.repository.create, identifier, user_id, filename, extension, size)
        try:
            await asyncio.to_thread(self.storage.reserve, identifier, size + 4096, f"uploads/{identifier}")
        except BaseException:
            await database_call(self.repository.terminal, identifier, user_id, "failed", "UPLOAD_CAPACITY_FAILED")
            raise
        return item

    async def get(self, identifier, user_id):
        item = await database_call(self.repository.get, identifier, user_id)
        if item["status"] == "validating" and item["task_id"] and self.tasks:
            task = await database_call(self.tasks.repository.get, item["task_id"])
            if task.status in {"failed", "cancelled", "expired"}:
                item = await database_call(self.repository.terminal, identifier, user_id, "failed", task.error_code)
        return item

    async def receive(self, identifier, user_id, chunks, content_length=None):
        await self.get(identifier, user_id)
        with self.files.lock(identifier):
            item = await self.get(identifier, user_id)
            UploadRepository.available(SimpleNamespace(**item), {"pending"})
            if content_length is not None and content_length != item["expected_bytes"]:
                raise AppException("UPLOAD_SIZE_MISMATCH", "请求长度与声明的文件大小不一致", 422)
            directory = self.files.directory(identifier)
            directory.mkdir(mode=0o700, exist_ok=True)
            partial = directory / "source.part"
            # A previous request may have promoted bytes before its DB acknowledgement was lost.
            self.files.path(identifier).unlink(missing_ok=True)
            total, digest, promoted = 0, hashlib.sha256(), False
            try:
                fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    async with asyncio.timeout(settings.UPLOAD_TOTAL_TIMEOUT_SECONDS):
                        iterator = chunks.__aiter__()
                        while True:
                            try:
                                chunk = await asyncio.wait_for(anext(iterator), settings.UPLOAD_IDLE_TIMEOUT_SECONDS)
                            except StopAsyncIteration:
                                break
                            total += len(chunk)
                            if total > item["expected_bytes"]:
                                raise AppException("FILE_TOO_LARGE", "实际上传超过声明的文件大小", 413)
                            await asyncio.to_thread(output.write, chunk)
                            digest.update(chunk)
                            await asyncio.to_thread(self.storage.check_capacity)
                        if total != item["expected_bytes"]:
                            raise AppException("UPLOAD_SIZE_MISMATCH", "上传不完整，请重新上传", 422)
                        await asyncio.to_thread(output.flush)
                        await asyncio.to_thread(os.fsync, output.fileno())
                os.replace(partial, self.files.path(identifier))
                promoted = True
                result = await database_call(self.repository.received, identifier, user_id, total, digest.hexdigest())
                self.storage.release_reservation(identifier)
                return result
            except TimeoutError:
                raise AppException("UPLOAD_TIMEOUT", "上传超时，请重新上传", 408) from None
            except OSError:
                raise AppException("UPLOAD_WRITE_FAILED", "上传文件写入失败，请稍后重试", 503) from None
            finally:
                partial.unlink(missing_ok=True)
                # Preserve promoted bytes if DB commit/acknowledgement is uncertain. GET can recover them.
                if not promoted:
                    self.files.path(identifier).unlink(missing_ok=True)

    async def complete(self, identifier, user_id):
        if self.tasks is None:
            raise RuntimeError("TaskService is required")
        item = await self.get(identifier, user_id)
        UploadRepository.available(SimpleNamespace(**item), {"uploaded", "validating", "ready", "failed"})
        if item["task_id"]:
            task = await database_call(self.tasks.repository.get, item["task_id"])
        else:
            # Multiple confirmations may share the bytes. The DB row lock binds exactly one task.
            with self.files.lock(identifier, shared=True):
                item = await self.get(identifier, user_id)
                UploadRepository.available(SimpleNamespace(**item), {"uploaded", "validating", "ready", "failed"})
                self.files.valid_file(item)
                from app.modules.auth.dependencies import task_user_id
                marker = task_user_id.set(user_id)
                try:
                    task = await self.tasks.create("media_probe", input_payload={"upload_id": str(identifier)})
                finally:
                    task_user_id.reset(marker)
        return {"upload_id": identifier, "task_id": task.id, "status": task.status,
                "events_url": f"/api/v1/tasks/{task.id}/events"}

    async def resolve(self, identifier, user_id):
        if user_id is None:
            raise AppException("AUTH_REQUIRED", "请登录后访问上传文件", 401)
        await self.get(identifier, user_id)
        with self.files.lock(identifier, shared=True):
            item = await self.get(identifier, user_id)
            UploadRepository.available(SimpleNamespace(**item), {"ready"})
            self.files.valid_file(item)
            return {"source_kind": "upload", "upload_id": str(identifier), "video_id": str(item["video_id"]),
                    "title": item["filename"], "media_type": item["media_type"],
                    "duration_seconds": item["duration_seconds"], "size_bytes": item["size_bytes"],
                    "content_sha256": item["content_sha256"]}

    async def delete(self, identifier, user_id):
        await self.get(identifier, user_id)
        with self.files.lock(identifier):
            await self.get(identifier, user_id)
            if await self.in_use(identifier):
                raise AppException("UPLOAD_IN_USE", "文件有未结束的任务，请先取消任务后删除", 409)
            # Hide the source first. A crash before unlink remains recoverable by cleanup.
            item = await database_call(self.repository.terminal, identifier, user_id, "deleted")
            await asyncio.to_thread(self.files.remove, identifier)
            await database_call(self.repository.mark_purged, identifier, user_id)
            return item

    async def in_use(self, identifier):
        if self.tasks is None:
            return False
        if await database_call(self.tasks.repository.has_active_upload, identifier):
            return True
        from app.infrastructure.process_runner import ProcessRunner
        for task_id in await database_call(self.tasks.repository.upload_task_ids, identifier):
            if not ProcessRunner.directory_stopped(self.storage.root / "tmp" / str(task_id)):
                return True
        return False


def upload_service(sessions, storage=None, tasks=None):
    return UploadService(UploadRepository(sessions), storage or LocalStorageAdapter(settings.storage_path,
        settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB), tasks)
