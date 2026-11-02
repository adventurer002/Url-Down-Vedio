from contextlib import contextmanager, nullcontext
import fcntl
import json
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
        for name in ("tmp", "files", "uploads", ".reservations", ".locks"):
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
        pending = self.reserved_bytes()
        if self.used_bytes() + pending + required_bytes >= self.quota_bytes:
            raise AppException("STORAGE_QUOTA_EXCEEDED", "存储配额不足，请先清理过期文件", 507)
        if shutil.disk_usage(self.root).free < self.min_free_bytes + pending + required_bytes:
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
        from app.infrastructure.process_runner import ProcessRunner
        if not ProcessRunner.directory_stopped(directory):
            raise AppException("PROCESS_CLEANUP_FAILED", "任务仍有存活或状态不明的进程，已保留临时文件", 500)
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

    def promote_batch(self, task_id: UUID, files: list[tuple[Path, UUID, str]], *, locked=False) -> list[str]:
        """Short commit lock; moved bytes no longer consume the task's unallocated reservation."""
        marker = self.root / ".reservations" / f"{task_id}.json"
        with nullcontext() if locked else self.lease():
            data = json.loads(marker.read_text())
            moved = list(data.get("promoted", []))
            # Persist destinations BEFORE rename: a crash after any rename remains discoverable.
            destinations = [f"files/{identifier}.{extension}" for _, identifier, extension in files]
            manifest = self.root / "tmp" / str(task_id) / ".clip-commit.json"
            temporary = manifest.with_suffix(".part")
            temporary.write_text(json.dumps({"files": destinations}))
            os.replace(temporary, manifest)
            output = []
            for source, identifier, extension in files:
                key = self.promote(task_id, source, identifier, extension)
                moved.append(key)
                data["promoted"] = moved
                temporary = marker.with_suffix(".part")
                temporary.write_text(json.dumps(data))
                os.replace(temporary, marker)
                output.append(key)
            return output

    def discard_batch(self, task_id: UUID, registered: set[str]) -> None:
        """Caller has proved the task/process stopped and checked durable registrations."""
        from app.infrastructure.process_runner import ProcessRunner
        if not ProcessRunner.directory_stopped(self.root / "tmp" / str(task_id)):
            raise AppException("PROCESS_CLEANUP_FAILED", "进程退出未确认，已保留提交清单和媒体文件", 500)
        manifest = self.root / "tmp" / str(task_id) / ".clip-commit.json"
        if not manifest.exists():
            return
        data = json.loads(manifest.read_text())
        for key in data["files"]:
            if key not in registered:
                self.remove_file(key)

    def reserved_bytes(self) -> int:
        """Only unallocated bytes count; already-written files count in used_bytes."""
        from app.infrastructure.process_runner import directory_size
        total = 0
        for marker in (self.root / ".reservations").glob("*.json"):
            try:
                data = json.loads(marker.read_text())
                relative = Path(data["path"])
                if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"uploads", "tmp"}:
                    raise ValueError
                promoted = 0
                for key in data.get("promoted", []):
                    path = self.resolve_file(key)
                    if path.is_file():
                        promoted += path.stat().st_size
                total += max(0, int(data["bytes"]) - directory_size(self.root / relative) - promoted)
            except FileNotFoundError:
                continue
            except (ValueError, KeyError, TypeError):
                raise AppException("STORAGE_RESERVATION_INVALID", "存储预留记录无效，请联系管理员", 503) from None
        return total

    def reserve(self, identifier: UUID, required_bytes: int, relative_path: str):
        marker = self.root / ".reservations" / f"{UUID(str(identifier))}.json"
        with self.lease():
            if marker.exists():
                raise AppException("STORAGE_BUSY", "存储预留已存在", 409)
            self.check_capacity(required_bytes)
            temporary = marker.with_suffix(".part")
            try:
                with temporary.open("w") as stream:
                    json.dump({"bytes": required_bytes, "path": relative_path}, stream)
                os.replace(temporary, marker)
            finally:
                temporary.unlink(missing_ok=True)

    def release_reservation(self, identifier: UUID):
        (self.root / ".reservations" / f"{UUID(str(identifier))}.json").unlink(missing_ok=True)

    @contextmanager
    def reservation(self, identifier: UUID, required_bytes: int):
        self.reserve(identifier, required_bytes, f"tmp/{identifier}")
        try:
            yield
        finally:
            from app.infrastructure.process_runner import ProcessRunner
            if ProcessRunner.directory_stopped(self.root / "tmp" / str(identifier)):
                self.release_reservation(identifier)

    @contextmanager
    def file_lock(self, path: Path, *, shared=True):
        if path.parent != self.root / "files" or not re.fullmatch(r"[0-9a-f-]{36}\.(mp4|mp3)", path.name):
            raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
        fd = os.open(self.root / ".locks" / f"media-{path.name}", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppException("STORAGE_BUSY", "媒体文件正在使用，请稍后重试", 503) from None
            yield
        finally:
            os.close(fd)
