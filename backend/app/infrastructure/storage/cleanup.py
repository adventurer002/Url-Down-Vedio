from datetime import timedelta
import json
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.storage.base import StorageAdapter
from app.infrastructure.storage.local import LocalStorageAdapter
from app.infrastructure.storage.retry import wait_for_storage
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.service import database_call


async def protected_clip_sources(storage, tasks, media):
    """Build once per cleanup lease. Corrupt pins quarantine only known task dependencies."""
    from app.infrastructure.process_runner import ProcessRunner
    protected = set()
    for pin in (storage.root / "tmp").glob("*/.clip-source.json"):
        key = None
        try:
            data = json.loads(pin.read_text())
            key = data["storage_key"]
            if not isinstance(key, str) or not key.startswith("files/"):
                key = None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            task = await database_call(tasks.get, UUID(pin.parent.name))
        except (ValueError, AppException) as error:
            if isinstance(error, AppException) and error.code != "TASK_NOT_FOUND":
                raise
            task = None
        active = task is not None and task.status in {"queued", "running", "processing"}
        stopped = ProcessRunner.directory_stopped(pin.parent)
        if key and (active or not stopped or task is None):
            protected.add(key)
        elif key is None and (active or not stopped or task is None):
            # The task directory remains quarantined by process recovery. A known video
            # narrows source protection; an unknown task cannot pin every user's media.
            if task is not None and task.video_id:
                protected.update(await database_call(media.source_keys_for_video, task.video_id))
    return protected


async def clip_source_in_use(storage, tasks, storage_key):
    return storage_key in await protected_clip_sources(storage, tasks, MediaRepository(tasks.sessions))


async def cleanup_expired_files(storage: StorageAdapter | None = None, media: MediaRepository | None = None,
                                tasks: TaskRepository | None = None, *, is_cancel_requested=None, on_wait=None) -> dict:
    storage = storage or LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
    media = media or MediaRepository(SessionLocal)
    tasks = tasks or TaskRepository(SessionLocal)
    now = utcnow()
    cutoff = (now - timedelta(hours=settings.STORAGE_FILE_TTL_HOURS)).timestamp()
    counts = {"files": 0, "temporary_directories": 0, "orphan_files": 0}
    async with wait_for_storage(storage.lease, is_cancel_requested=is_cancel_requested, on_wait=on_wait):
        before_bytes = storage.used_bytes()
        protected_sources = await protected_clip_sources(storage, tasks, media)
        for item in await database_call(media.expired, now):
            if item.storage_key in protected_sources:
                continue
            try:
                with storage.file_lock(storage.resolve_file(item.storage_key), shared=False):
                    storage.remove_file(item.storage_key)
                    await database_call(media.delete_expired, item.id, now)
                    counts["files"] += 1
            except AppException as error:
                if error.code != "STORAGE_BUSY":
                    raise
        for folder in (storage.root / "tmp").iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                task_id = UUID(folder.name)
            except ValueError:
                continue
            from app.infrastructure.process_runner import ProcessRunner
            if not ProcessRunner.directory_stopped(folder):
                continue
            task = None
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
                storage.discard_batch(task_id, await database_call(media.storage_keys))
                storage.remove_task_directory(task_id)
                storage.release_reservation(task_id)
                if task is not None:
                    from app.modules.tasks.execution import discard_receipt
                    discard_receipt(storage.root, task)
                counts["temporary_directories"] += 1
        registered = await database_call(media.storage_keys)
        for path in (storage.root / "files").iterdir():
            key = f"files/{path.name}"
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff and key not in registered:
                if key in protected_sources:
                    continue
                try:
                    storage.remove_file(key)
                    counts["orphan_files"] += 1
                except AppException as exc:
                    if exc.code != "FILE_NOT_FOUND":
                        raise
        from app.modules.uploads.cleanup import cleanup_uploads
        counts.update(await cleanup_uploads(storage, tasks, cutoff))
        from app.modules.tasks.execution import cleanup_attempt_receipts
        await cleanup_attempt_receipts(storage.root, tasks)
        after_bytes = storage.used_bytes()
        counts.update(freed_bytes=max(0, before_bytes - after_bytes), used_bytes=after_bytes)
    return counts


if __name__ == "__main__":
    import asyncio
    print(asyncio.run(cleanup_expired_files()))
