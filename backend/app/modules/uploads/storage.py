from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
from uuid import UUID

from app.core.exceptions import AppException


class UploadStorage:
    def __init__(self, storage):
        self.storage = storage

    def directory(self, upload_id):
        directory = self.storage.root / "uploads" / str(UUID(str(upload_id)))
        if directory.is_symlink():
            raise AppException("UPLOAD_NOT_FOUND", "上传文件不存在", 404)
        return directory

    def path(self, upload_id):
        path = self.directory(upload_id) / "source.bin"
        if path.is_symlink():
            raise AppException("UPLOAD_NOT_FOUND", "上传文件不存在", 404)
        return path

    @contextmanager
    def lock(self, upload_id, *, shared=False):
        # Stable lock inode outside removable upload directories.
        name = self.storage.root / ".locks" / f"upload-{UUID(str(upload_id))}"
        fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppException("UPLOAD_IN_USE", "文件正在上传或处理中，请取消任务后重试", 409) from None
            yield
        finally:
            os.close(fd)

    def remove(self, upload_id):
        directory = self.directory(upload_id)
        if directory.exists():
            shutil.rmtree(directory)
        self.storage.release_reservation(upload_id)

    def valid_file(self, record):
        path = self.path(record["id"])
        if not path.is_file() or path.stat().st_size != record["size_bytes"]:
            raise AppException("UPLOAD_FILE_MISSING", "上传文件已丢失或已过期，请重新上传", 410)
        return path
