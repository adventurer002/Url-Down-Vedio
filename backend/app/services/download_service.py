"""Download scheduling, state machine, cancel, file delivery, expiry cleanup."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.db.models import DownloadTask, MediaFile, Video

logger = structlog.get_logger(__name__)

FORWARD = ["queued", "downloading", "processing", "uploading", "completed"]
TERMINAL = {"completed", "failed", "cancelled"}
CANCELLABLE = {"queued", "downloading", "processing"}


def advance(task: DownloadTask, new_status: str) -> None:
    """Forward-only transitions; failed/cancelled are terminal sinks."""
    if task.status in TERMINAL:
        raise AppError("internal_error", f"终态任务不可流转: {task.status}")
    if new_status in ("failed", "cancelled"):
        task.status = new_status
        return
    if new_status not in FORWARD or FORWARD.index(new_status) <= FORWARD.index(task.status):
        raise AppError("internal_error", f"非法状态流转: {task.status} -> {new_status}")
    task.status = new_status


def _now() -> datetime:
    return datetime.now(UTC)


async def create_download_task(
    session: AsyncSession,
    video_id: uuid.UUID,
    format_id: str,
    user_id: uuid.UUID | None,
    celery_delay: Callable[[str], str | None],
) -> DownloadTask:
    video = await session.get(Video, video_id)
    if video is None or (user_id is not None and video.user_id not in (None, user_id)):
        raise AppError("not_found", "视频不存在")
    if video.status != "ready":
        raise AppError("download_failed", "视频尚未解析完成", {"video_status": video.status})
    quality = format_id
    for f in video.formats or []:
        if isinstance(f, dict) and f.get("format_id") == format_id:
            quality = str(f.get("resolution") or format_id)
            break
    task = DownloadTask(
        user_id=user_id,
        video_id=video_id,
        format_id=format_id,
        quality=quality,
        status="queued",
        progress=0,
        celery_task_id="",
        started_at=_now(),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    celery_task_id = celery_delay(str(task.id))
    if celery_task_id:
        task.celery_task_id = celery_task_id
        await session.commit()
        await session.refresh(task)
    return task


async def request_cancel(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: uuid.UUID | None,
    revoke: Callable[[str], None],
    set_cancel_flag: Callable[[uuid.UUID], None],
) -> DownloadTask:
    task = await session.get(DownloadTask, task_id)
    if task is None or (user_id is not None and task.user_id not in (None, user_id)):
        raise AppError("not_found", "任务不存在")
    if task.status not in CANCELLABLE:
        raise AppError("task_not_cancellable", "任务当前状态不可取消", {"status": task.status})
    set_cancel_flag(task.id)
    if task.celery_task_id:
        revoke(task.celery_task_id)
    return task


async def task_media_file(session: AsyncSession, task_id: uuid.UUID) -> MediaFile | None:
    rows = (
        await session.execute(
            select(MediaFile).where(MediaFile.task_id == task_id).order_by(MediaFile.created_at.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


async def resolve_file(
    session: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID | None
) -> MediaFile:
    task = await session.get(DownloadTask, task_id)
    if task is None or (user_id is not None and task.user_id not in (None, user_id)):
        raise AppError("not_found", "任务不存在")
    if task.status != "completed":
        raise AppError("download_failed", "任务尚未完成", {"status": task.status})
    media = await task_media_file(session, task_id)
    if media is None or media.deleted_at is not None:
        raise AppError("not_found", "文件不存在或已被清理")
    if media.expires_at <= _now():
        raise AppError("not_found", "文件已过期")
    return media


async def cleanup_expired(
    session: AsyncSession, delete_object: Callable[[str], Any]
) -> int:
    """Delete expired media objects; returns removed count. Service never touches OSS directly."""
    rows = (
        await session.execute(
            select(MediaFile).where(
                MediaFile.deleted_at.is_(None), MediaFile.expires_at <= _now()
            )
        )
    ).scalars().all()
    for media in rows:
        try:
            res = delete_object(media.storage_key)
            if isawaitable(res):
                await res
        except Exception as e:  # noqa: BLE001 - storage backends raise heterogeneous errors
            logger.warning("cleanup_delete_failed", key=media.storage_key, error=str(e))
            continue
        media.deleted_at = _now()
    await session.commit()
    await session.execute(
        delete(DownloadTask).where(
            DownloadTask.status.in_(("failed", "cancelled")),
            DownloadTask.finished_at.is_not(None),
            DownloadTask.finished_at <= _now() - timedelta(days=30),
        )
    )
    await session.commit()
    return len(rows)


def temp_work_dir(root: Path, task_id: uuid.UUID) -> Path:
    d = root / str(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return d
