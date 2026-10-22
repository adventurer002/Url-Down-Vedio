from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
from uuid import UUID

from app.core.exceptions import AppException


class LocalStorageAdapter:
    def __init__(self, root: Path, quota_mb: int = 5120, min_free_mb: int = 256):
        self.root = Path(root).resolve()
        self.quota_bytes = quota_mb * 1024 * 1024
        self.min_free_bytes = min_free_mb * 1024 * 1024
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("tmp", "files"):
            folder = self.root / name
            if folder.is_symlink():
                raise AppException("INVALID_STORAGE_PATH", "存储目录配置无效", 500)
            folder.mkdir(exist_ok=True)

    @contextmanager
    def lease(self):
        # Serialize admissions, promotions and cleanup across API/Worker processes.
        fd = os.open(self.root / "tmp" / ".quota.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppException("STORAGE_BUSY", "存储正在使用，请稍后重试", 503) from None
            yield
        finally:
            os.close(fd)

    def used_bytes(self) -> int:
        total = 0
        for folder, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
            for name in files:
                path = Path(folder) / name
                try:
                    total += path.lstat().st_size
                except FileNotFoundError:
                    pass
        return total

    def check_capacity(self, required_bytes: int = 0) -> None:
        if self.used_bytes() + required_bytes >= self.quota_bytes:
            raise AppException("STORAGE_QUOTA_EXCEEDED", "存储配额不足，请先清理过期文件", 507)
        if shutil.disk_usage(self.root).free < self.min_free_bytes + required_bytes:
            raise AppException("INSUFFICIENT_DISK_SPACE", "磁盘可用空间不足", 507)

    def task_directory(self, task_id: UUID) -> Path:
        directory = self.root / "tmp" / str(UUID(str(task_id)))
        if directory.is_symlink():
            raise AppException("INVALID_STORAGE_PATH", "任务存储目录无效", 500)
        return directory

    def create_task_directory(self, task_id: UUID) -> Path:
        directory = self.task_directory(task_id)
        directory.mkdir(mode=0o700, exist_ok=False)
        return directory

    def remove_task_directory(self, task_id: UUID) -> None:
        directory = self.task_directory(task_id)
        if directory.exists():
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass

    def resolve_file(self, storage_key: str) -> Path:
        if not re.fullmatch(r"files/[0-9a-f-]{36}\.(mp4|mp3)", storage_key):
            raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
        path = self.root / storage_key
        if path.is_symlink() or path.resolve().parent != self.root / "files":
            raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
        return path

    def promote(self, task_id: UUID, source: Path, file_id: UUID, extension: str) -> str:
        if extension not in {"mp4", "mp3"} or source.is_symlink() or source.resolve().parent != self.task_directory(task_id):
            raise AppException("INVALID_STORAGE_PATH", "下载输出路径无效", 500)
        key = f"files/{UUID(str(file_id))}.{extension}"
        destination = self.resolve_file(key)
        if destination.exists():
            raise AppException("STORAGE_CONFLICT", "目标文件已存在", 500)
        os.replace(source, destination)
        return key

    def remove_file(self, storage_key: str) -> None:
        self.resolve_file(storage_key).unlink(missing_ok=True)
