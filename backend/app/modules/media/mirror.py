"""Copy verified local deliverables to the private bucket without blocking the render queue."""
import asyncio
import logging
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.storage import LocalStorageAdapter
from app.infrastructure.storage.objects import object_key, object_store
from app.infrastructure.timing import stage_timer
from app.modules.media.repository import MediaRepository
from app.modules.tasks.execution import execution_lock
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import database_call

MIRROR_SLOT = UUID("e5d1c93e-8fd6-4425-8da2-e72f374ccabe")
logger = logging.getLogger(__name__)


async def mirror_file(file_id, repository, storage, store):
    # Shared across both Solo media consumers, with no storage quota lock held during I/O.
    try:
        with execution_lock(storage.root, MIRROR_SLOT):
            if not await database_call(repository.begin_mirror, file_id, utcnow()):
                return False
            record = await database_call(repository.get, file_id)
            path = storage.resolve_file(record.storage_key)
            with storage.file_lock(path):
                if not path.is_file() or path.stat().st_size != record.size_bytes:
                    return False
                with stage_timer("object_upload", record.task_id):
                    await store.put(object_key(store.tag, record.storage_key), path, record.size_bytes, record.mime_type)
                return await database_call(repository.finish_mirror, file_id, store.tag, utcnow())
    except AppException as error:
        if error.code != "TASK_EXECUTION_BUSY":
            logger.warning("对象镜像等待补发 file_id=%s error_code=%s", file_id, error.code)
        return False


def enqueue_mirror(file_id):
    queue = "media" if settings.STAGED_TASKS_ENABLED else "celery"
    celery_app.send_task("objects.mirror", args=[str(file_id)], queue=queue)


async def schedule_mirror(file_id):
    if not settings.OBJECT_MIRROR_ENABLED or file_id is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(enqueue_mirror, file_id), timeout=5)
    except Exception:
        logger.warning("对象镜像派发待恢复 file_id=%s", file_id)


async def recover_mirrors(repository):
    if settings.OBJECT_MIRROR_ENABLED:
        await schedule_mirror(await database_call(repository.next_mirror, utcnow()))


@celery_app.task(name="objects.mirror", ignore_result=True)
def mirror_task(file_id):
    if not settings.OBJECT_MIRROR_ENABLED:
        return
    storage = LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
    try:
        store = object_store(upload=True)
    except AppException as error:
        logger.warning("对象镜像配置不可用 error_code=%s", error.code)
        return
    return asyncio.run(mirror_file(UUID(file_id), MediaRepository(SessionLocal), storage, store))
