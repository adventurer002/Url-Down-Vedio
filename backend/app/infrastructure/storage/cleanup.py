from datetime import timedelta
import json
import os
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.storage.base import StorageAdapter
from app.infrastructure.storage.local import LocalStorageAdapter
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.service import database_call


async def cleanup_expired_files(storage: StorageAdapter | None = None, media: MediaRepository | None = None,
                                tasks: TaskRepository | None = None) -> dict:
    storage = storage or LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
    media = media or MediaRepository(SessionLocal)
    tasks = tasks or TaskRepository(SessionLocal)
    now = utcnow()
    cutoff = (now - timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)).timestamp()
    counts = {"files": 0, "temporary_directories": 0, "orphan_files": 0}
    with storage.lease():
        for item in await database_call(media.expired, now):
            storage.remove_file(item.storage_key)
            await database_call(media.delete_expired, item.id, now)
            counts["files"] += 1
        for folder in (storage.root / "tmp").iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                task_id = UUID(folder.name)
            except ValueError:
                continue
            marker = folder / ".process.json"
            if marker.exists():
                try:
                    pgid = json.loads(marker.read_text(encoding="utf-8"))["pgid"]
                    if not isinstance(pgid, int) or pgid <= 1:
                        continue
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    pass
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # Cannot prove the process tree is gone; preserve data.
                else:
                    continue
            try:
                task = await database_call(tasks.get, task_id)
                if task.status in {"queued", "running", "processing"} and task.expires_at:
                    task = await database_call(tasks.expire_stale, task_id)
                removable = task.status in {"completed", "failed", "cancelled", "expired"}
            except AppException as exc:
                if exc.code != "TASK_NOT_FOUND":
                    raise
                removable = folder.stat().st_mtime < cutoff
            if removable:
                storage.remove_task_directory(task_id)
                counts["temporary_directories"] += 1
        registered = await database_call(media.storage_keys)
        for path in (storage.root / "files").iterdir():
            key = f"files/{path.name}"
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff and key not in registered:
                try:
                    storage.remove_file(key)
                    counts["orphan_files"] += 1
                except AppException as exc:
                    if exc.code != "FILE_NOT_FOUND":
                        raise
    return counts


if __name__ == "__main__":
    import asyncio
    print(asyncio.run(cleanup_expired_files()))
