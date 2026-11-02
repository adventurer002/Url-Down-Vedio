from uuid import UUID

from app.core.exceptions import AppException
from app.modules.billing.policy import now
from app.modules.tasks.service import database_call
from app.modules.uploads.service import upload_service


async def cleanup_uploads(storage, tasks, cutoff):
    # Called under the short global storage lease. Per-source locks protect active readers.
    from app.modules.tasks.service import TaskService
    uploads = upload_service(tasks.sessions, storage, TaskService(tasks, None))
    counts = {"uploads": 0, "orphan_uploads": 0, "reservations": 0}
    instant, after = now(), None
    while True:
        batch = await database_call(uploads.repository.cleanup_candidates, instant, 100, after)
        if not batch:
            break
        # Advance over skipped rows, and over deleted rows without using a shifting OFFSET.
        after = batch[-1]["expires_at"], batch[-1]["id"]
        for item in batch:
            identifier = item["id"]
            try:
                with uploads.files.lock(identifier):
                    if await uploads.in_use(identifier):
                        continue
                    if not await database_call(uploads.repository.claim_cleanup, identifier, item["user_id"], instant):
                        continue
                    uploads.files.remove(identifier)
                    await database_call(uploads.repository.mark_purged, identifier, item["user_id"])
                    counts["uploads"] += 1
            except AppException as error:
                if error.code != "UPLOAD_IN_USE":
                    raise
    registered = await database_call(uploads.repository.storage_ids)
    for folder in (storage.root / "uploads").iterdir():
        if folder.is_symlink() or not folder.is_dir() or folder.name in registered or folder.stat().st_mtime >= cutoff:
            continue
        try:
            identifier = UUID(folder.name)
        except ValueError:
            continue
        try:
            with uploads.files.lock(identifier):
                uploads.files.remove(identifier)
                counts["orphan_uploads"] += 1
        except AppException as error:
            if error.code != "UPLOAD_IN_USE":
                raise
    # Release reservations left by a dead task only after its working directory was safely removed.
    pending_uploads = await database_call(uploads.repository.pending_reservation_ids)
    for marker in (storage.root / ".reservations").glob("*.json"):
        import json
        data = json.loads(marker.read_text())
        if data["path"].startswith("uploads/") and marker.stem not in pending_uploads:
            try:
                with uploads.files.lock(UUID(marker.stem)):
                    storage.release_reservation(UUID(marker.stem))
                    counts["reservations"] += 1
            except AppException as error:
                if error.code != "UPLOAD_IN_USE":
                    raise
        elif data["path"].startswith("tmp/") and not (storage.root / data["path"]).exists():
            task_id = UUID(marker.stem)
            try:
                task = await database_call(tasks.get, task_id)
            except AppException as error:
                if error.code != "TASK_NOT_FOUND":
                    raise
                if marker.stat().st_mtime >= cutoff:
                    continue
            else:
                if task.status in {"queued", "running", "processing"}:
                    continue
            storage.release_reservation(task_id)
            counts["reservations"] += 1
    return counts
