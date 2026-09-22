"""Celery task entrypoints. Thin wrappers: build real providers, run service logic."""

import asyncio
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis as sync_redis
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core import url_safety
from backend.app.core.config import settings
from backend.app.core.errors import AppError
from backend.app.db.models import DownloadTask, MediaFile, Video
from backend.app.providers.downloader import (
    DownloadCancelled,
    Downloader,
    DownloadProgress,
)
from backend.app.providers.ffmpeg import FFmpegCancelled, FFmpegError, remux_to_mp4
from backend.app.providers.storage import (
    LocalStorageProvider,
    OSSStorageProvider,
    StorageProvider,
    build_key,
)
from backend.app.providers.ytdlp import PublicDownloadError, YTDLPDownloader
from backend.app.services import download_service as downloads
from backend.app.services import video_service as videos
from backend.app.services.progress import ProgressReporter, cancel_requested
from backend.app.worker.celery_app import celery_app


def _session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def build_storage() -> StorageProvider:
    if settings.storage_backend == "oss":
        return OSSStorageProvider(
            settings.oss_endpoint,
            settings.oss_bucket,
            settings.oss_access_key,
            settings.oss_secret_key,
        )
    return LocalStorageProvider(Path(settings.storage_dir))


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- parse ---


async def _parse_impl(
    video_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    downloader: Downloader | None = None,
) -> None:
    maker = session_factory or _session_factory()
    dl = downloader or YTDLPDownloader()
    async with maker() as session:
        video = await session.get(Video, video_id)
        if video is None or video.status != "parsing":
            return
        try:
            url_safety.validate_url(video.source_url)
            info = await dl.extract_info(video.source_url)
        except PublicDownloadError as e:
            await videos.fail_parsing(session, video_id, str(e))
            return
        await videos.finish_parsing(session, video_id, info)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.tasks.parse_video",
    soft_time_limit=50,
    time_limit=60,
    queue="default",
)
def parse_video(video_id_str: str) -> str:
    asyncio.run(_parse_impl(uuid.UUID(video_id_str)))
    return "ok"


# -------------------------------------------------------------- download ---


def _db_write_on_loop(
    loop: asyncio.AbstractEventLoop,
    maker: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    status: str,
    progress: float,
) -> None:
    async def _update() -> None:
        async with maker() as session:
            task = await session.get(DownloadTask, task_id)
            if task is None:
                return
            if task.status in ("completed", "failed", "cancelled"):
                return  # terminal state wins; stale progress must not overwrite it
            if progress > float(task.progress or 0):
                task.progress = progress
            if task.status != status and status not in ("completed", "failed", "cancelled"):
                try:
                    downloads.advance(task, status)
                except AppError:
                    pass  # out-of-order progress write loses to the newer state
            await session.commit()

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        # Hook runs on the loop thread (e.g. in-process fakes): schedule, don't block.
        loop.create_task(_update())
    else:
        fut = asyncio.run_coroutine_threadsafe(_update(), loop)
        fut.result(timeout=10)


async def _download_impl(
    task_id: uuid.UUID,
    celery_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    downloader: Downloader | None = None,
    storage: StorageProvider | None = None,
) -> None:
    maker = session_factory or _session_factory()
    dl = downloader or YTDLPDownloader()
    store = storage or build_storage()
    redis_client = sync_redis.Redis.from_url(settings.redis_url)
    loop = asyncio.get_running_loop()

    async with maker() as session:
        task = await session.get(DownloadTask, task_id)
        if task is None or task.status == "cancelled":
            return
        if task.status != "queued":
            return
        video = await session.get(Video, task.video_id)
        if video is None:
            return
        try:
            url_safety.validate_url(video.source_url)
        except AppError as e:
            task.status = "failed"
            task.error_code = e.code
            task.error_message = e.message
            task.finished_at = _now()
            await session.commit()
            return
        task.celery_task_id = celery_id
        downloads.advance(task, "downloading")
        await session.commit()

        reporter = ProgressReporter(redis_client, task.id)
        tmp_root = Path(settings.worker_tmp_dir)
        work_dir = downloads.temp_work_dir(tmp_root, task.id)

        def should_cancel() -> bool:
            return cancel_requested(redis_client, task.id)

        def on_progress(p: DownloadProgress) -> None:
            reporter.report(
                "downloading",
                round(p.percent, 2),
                lambda s, v: _db_write_on_loop(loop, maker, task.id, s, v),
            )

        try:
            raw_path = await dl.download(
                video.source_url, task.format_id, work_dir,
                on_progress, should_cancel, None, None,
            )
            if should_cancel():
                raise DownloadCancelled()
            downloads.advance(task, "processing")
            await session.commit()
            final_path = remux_to_mp4(
                raw_path, work_dir / f"{task.id}.mp4", should_cancel
            )
            if should_cancel():
                raise DownloadCancelled()
            downloads.advance(task, "uploading")
            task.progress = 100
            await session.commit()
            reporter.report("uploading", 100.0, None)

            user_part = str(task.user_id) if task.user_id else "anon"
            key = build_key(settings.app_env, user_part, "video", final_path.suffix)
            await store.upload(final_path, key)
            media = MediaFile(
                owner_user_id=task.user_id,
                task_id=task.id,
                video_id=task.video_id,
                kind="video",
                format=final_path.suffix.lstrip(".").lower() or "mp4",
                size_bytes=final_path.stat().st_size,
                duration_seconds=video.duration_seconds,
                storage_backend=settings.storage_backend,
                storage_key=key,
                expires_at=_now()
                + timedelta(hours=settings.file_ttl_hours_free),
            )
            session.add(media)
            downloads.advance(task, "completed")
            task.progress = 100
            task.finished_at = _now()
            await session.commit()
            reporter.final("completed")
        except (DownloadCancelled, FFmpegCancelled):
            task.status = "cancelled"
            task.finished_at = _now()
            await session.commit()
            reporter.final("cancelled")
        except (PublicDownloadError, FFmpegError) as e:
            code = e.code if isinstance(e, PublicDownloadError) else "ffmpeg_error"
            task.status = "failed"
            task.error_code = code
            task.error_message = str(e)
            task.finished_at = _now()
            await session.commit()
            reporter.final("failed")
            raise
        except SoftTimeLimitExceeded:
            task.status = "failed"
            task.error_code = "timeout"
            task.error_message = "下载超时"
            task.finished_at = _now()
            await session.commit()
            reporter.final("failed")
        except Exception as e:  # noqa: BLE001 - worker must record, never leak
            task.status = "failed"
            task.error_code = "download_failed"
            task.error_message = str(e)
            task.finished_at = _now()
            await session.commit()
            reporter.final("failed")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.tasks.download_video",
    soft_time_limit=1500,
    time_limit=1800,
    queue="download",
    max_retries=2,
    autoretry_for=(PublicDownloadError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 2},
)
def download_video(task_id_str: str) -> str:
    from celery import current_task

    task_id = uuid.UUID(task_id_str)
    try:
        asyncio.run(_download_impl(task_id, current_task.request.id or ""))
    except PublicDownloadError as e:
        if not e.retryable:
            return "failed"
        raise
    return "ok"


# -------------------------------------------------------------- cleanup ---


async def _cleanup_impl(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    storage: StorageProvider | None = None,
) -> int:
    maker = session_factory or _session_factory()
    store = storage or build_storage()
    async with maker() as session:
        return await downloads.cleanup_expired(session, store.delete)


async def _expire_impl(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, int]:
    from backend.app.services import billing as billing_service

    maker = session_factory or _session_factory()
    async with maker() as session:
        return await billing_service.expire_due(session)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.tasks.cleanup_expired_files",
    soft_time_limit=300,
    time_limit=600,
    queue="default",
)
def cleanup_expired_files() -> str:
    removed = asyncio.run(_cleanup_impl())
    return f"ok removed={removed}"


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.tasks.expire_subscriptions",
    soft_time_limit=300,
    time_limit=600,
    queue="default",
)
def expire_subscriptions() -> str:
    result = asyncio.run(_expire_impl())
    return f"ok expired={result}"
