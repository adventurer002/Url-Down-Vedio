import asyncio
import os
from pathlib import Path
import shutil
import time

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.storage.cleanup import cleanup_expired_files


class CleanupService:
    def run(self):
        async def bounded():
            async with asyncio.timeout(90):
                return await cleanup_expired_files()
        try:
            return asyncio.run(bounded())
        except TimeoutError:
            raise AppException("STORAGE_CLEANUP_TIMEOUT", "清理超时，可能已完成部分清理，请查询审计", 504) from None
        except OSError:
            raise AppException("STORAGE_CLEANUP_FAILED", "文件清理失败，请稍后重试", 503) from None


def get_cleanup_service():
    return CleanupService()


class StorageStatusService:
    def __init__(self, root=None, *, budget_seconds=1, max_entries=100000):
        self.root = Path(root if root is not None else settings.storage_path)
        self.budget_seconds, self.max_entries = budget_seconds, max_entries

    def snapshot(self):
        # Bounded local scan; no directories created and no paths exposed.
        deadline = time.monotonic() + self.budget_seconds
        size, entries, complete = 0, 0, True
        anchor = self.root
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        free = shutil.disk_usage(anchor).free

        def failed(_):
            nonlocal complete
            complete = False

        if self.root.is_dir():
            for folder, dirs, files in os.walk(self.root, followlinks=False, onerror=failed):
                dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
                for name in files:
                    entries += 1
                    if entries > self.max_entries or time.monotonic() > deadline:
                        complete = False
                        break
                    try:
                        size += (Path(folder) / name).lstat().st_size
                    except FileNotFoundError:
                        pass
                    except OSError:
                        complete = False
                if entries > self.max_entries or time.monotonic() > deadline:
                    complete = False
                    break
        return {"status": "ready" if complete else "partial", "used_bytes": size if complete else None,
            "observed_bytes": size, "disk_free_bytes": free, "scan_complete": complete,
            "quota_bytes": settings.STORAGE_MAX_QUOTA_MB * 1024**2,
            "min_free_bytes": settings.STORAGE_MIN_FREE_MB * 1024**2,
            "file_ttl_hours": settings.STORAGE_FILE_TTL_HOURS}


def get_storage_status_service():
    return StorageStatusService()
