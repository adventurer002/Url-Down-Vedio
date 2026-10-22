import asyncio
from uuid import UUID

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.queue.celery_app import celery_app
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.storage import LocalStorageAdapter
from app.modules.download.worker import execute_download
from app.modules.media.audio import AudioAcquirer
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService


class AudioDispatcher:
    def enqueue(self, task_id: UUID):
        celery_app.send_task("media.extract_audio", args=[str(task_id)], task_id=str(task_id))


@celery_app.task(name="media.extract_audio")
def extract_audio_task(task_id: str):
    async def execute():
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                               socket_timeout=1, retry=Retry(NoBackoff(), 0))
        try:
            storage = LocalStorageAdapter(settings.storage_path, settings.STORAGE_MAX_QUOTA_MB, settings.STORAGE_MIN_FREE_MB)
            media = MediaRepository(SessionLocal)
            audio = AudioAcquirer(media, storage, ProcessRunner(settings.DOWNLOAD_TIMEOUT_SECONDS, settings.MAX_FILE_SIZE_MB * 1024 * 1024))
            return await execute_download(UUID(task_id), TaskService(TaskRepository(SessionLocal), ProgressBus(redis)),
                                          media, storage, audio, reuse_result=audio.reuse_result)
        finally:
            await redis.aclose()

    return asyncio.run(execute())
