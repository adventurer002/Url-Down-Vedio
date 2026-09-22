"""Phase4 routes: parse + download gated on login; ownership enforced."""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import redis.asyncio as aredis
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth_deps import optional_user, require_user
from backend.app.api.deps import get_session
from backend.app.core.config import settings
from backend.app.core.errors import AppError
from backend.app.db.models import DownloadTask, User, Video
from backend.app.providers.storage import LocalStorageProvider
from backend.app.schemas.media import (
    DownloadRequest,
    DownloadResponse,
    FormatInfo,
    Page,
    ParseRequest,
    ParseResponse,
    TaskDetail,
    VideoDetail,
)
from backend.app.services import download_service as downloads
from backend.app.services import permission as perm
from backend.app.services import video_service as videos
from backend.app.services.progress import cancel_key, progress_channel

router = APIRouter(prefix="/api/v1")


def _video_out(video: Video) -> VideoDetail:
    formats = [
        FormatInfo(
            format_id=str(f.get("format_id", "")),
            ext=str(f.get("ext", "")),
            resolution=f.get("resolution"),
            filesize=f.get("filesize"),
            vcodec=f.get("vcodec"),
            acodec=f.get("acodec"),
        )
        for f in (video.formats or [])
        if isinstance(f, dict)
    ]
    return VideoDetail(
        id=video.id,
        title=video.title,
        uploader=video.uploader,
        thumbnail_url=video.thumbnail_url,
        duration_seconds=video.duration_seconds,
        platform=video.platform,
        webpage_url=video.webpage_url,
        formats=formats,
        status=video.status,
        error_message=video.error_message,
    )


async def _task_out(session: AsyncSession, task: DownloadTask) -> TaskDetail:
    media = await downloads.task_media_file(session, task.id)
    return TaskDetail(
        id=task.id,
        video_id=task.video_id,
        format_id=task.format_id,
        quality=task.quality,
        status=task.status,
        progress=float(task.progress),
        error_code=task.error_code,
        error_message=task.error_message,
        media_file_id=media.id if media else None,
        created_at=task.created_at,
        finished_at=task.finished_at,
    )


@router.post("/videos/parse", response_model=ParseResponse, status_code=202)
async def parse_video_endpoint(
    body: ParseRequest,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_user),
) -> ParseResponse:
    from backend.app.worker import tasks as worker_tasks

    user_id = user.id if user else None
    video = await videos.create_parsing_video(session, body.url, user_id)
    videos.schedule_parse(lambda vid: worker_tasks.parse_video.delay(vid).id, video.id)
    return ParseResponse(video_id=video.id)


@router.get("/videos/{video_id}", response_model=VideoDetail)
async def get_video(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_user),
) -> VideoDetail:
    video = await session.get(Video, video_id)
    if video is None:
        raise AppError("not_found", "视频不存在")
    if user is None or (video.user_id is not None and video.user_id != user.id):
        raise AppError("not_found", "视频不存在")
    return _video_out(video)


@router.get("/videos", response_model=Page)
async def list_videos(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page:
    stmt = select(Video).where(Video.user_id == user.id).order_by(Video.created_at.desc())
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return Page(
        items=[_video_out(v) for v in rows], total=total, page=page, page_size=page_size
    )


@router.post("/downloads", response_model=DownloadResponse, status_code=202)
async def create_download(
    body: DownloadRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    idempotency_key: str | None = Header(default=None),
) -> DownloadResponse:
    from backend.app.worker import tasks as worker_tasks

    video = await session.get(Video, body.video_id)
    if video is None or (video.user_id is not None and video.user_id != user.id):
        raise AppError("not_found", "视频不存在")
    check = await perm.can_download(session, user.id, video.duration_seconds)
    if not check.allowed:
        raise AppError(check.code, check.message)
    if idempotency_key:
        r = aredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        cached = await r.get(f"idem:{user.id}:{idempotency_key}")
        await r.aclose()
        if cached:
            return DownloadResponse(task_id=uuid.UUID(cached.decode()))
    task = await downloads.create_download_task(
        session,
        body.video_id,
        body.format_id,
        user.id,
        lambda tid: worker_tasks.download_video.delay(tid).id,
    )
    await perm.record_download_usage(session, user.id, task.id)
    if idempotency_key:
        r = aredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        await r.set(f"idem:{user.id}:{idempotency_key}", str(task.id), ex=86400)
        await r.aclose()
    return DownloadResponse(task_id=task.id)


@router.get("/downloads")
async def list_downloads(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page:
    stmt = (
        select(DownloadTask)
        .where(DownloadTask.user_id == user.id)
        .order_by(DownloadTask.created_at.desc())
    )
    if status:
        stmt = stmt.where(DownloadTask.status == status)
    total = (
        await session.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return Page(
        items=[await _task_out(session, t) for t in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/downloads/{task_id}", response_model=TaskDetail)
async def get_download(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TaskDetail:
    task = await session.get(DownloadTask, task_id)
    if task is None or task.user_id != user.id:
        raise AppError("not_found", "任务不存在")
    return await _task_out(session, task)


@router.post("/downloads/{task_id}/cancel", response_model=TaskDetail)
async def cancel_download(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TaskDetail:
    from backend.app.worker.celery_app import celery_app

    async def _revoke_and_flag() -> DownloadTask:
        def _revoke(celery_id: str) -> None:
            celery_app.control.revoke(celery_id, terminate=True)

        def _flag(tid: uuid.UUID) -> None:
            import redis as sync_redis

            r = sync_redis.Redis.from_url(settings.redis_url)
            r.set(cancel_key(tid), "1", ex=3600)
            r.close()

        return await downloads.request_cancel(session, task_id, user.id, _revoke, _flag)

    task = await _revoke_and_flag()
    return await _task_out(session, task)


@router.get("/downloads/{task_id}/events")
async def download_events(
    task_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> StreamingResponse:
    task = await session.get(DownloadTask, task_id)
    if task is None or task.user_id != user.id:
        raise AppError("not_found", "任务不存在")

    async def gen() -> AsyncIterator[str]:
        yield f"event: progress\ndata: {json.dumps({'task_id': str(task.id), 'status': task.status, 'progress': float(task.progress)})}\n\n"
        if task.status in ("completed", "failed", "cancelled"):
            media = await downloads.task_media_file(session, task.id)
            yield f"event: end\ndata: {json.dumps({'task_id': str(task.id), 'status': task.status, 'media_file_id': str(media.id) if media else None})}\n\n"
            return
        r = aredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        pubsub = r.pubsub()
        await pubsub.subscribe(progress_channel(task.id))
        try:
            async for msg in pubsub.listen():
                if await request.is_disconnected():
                    break
                if msg.get("type") != "message":
                    continue
                payload = json.loads(msg["data"])
                if payload.get("end"):
                    media = await downloads.task_media_file(session, task.id)
                    yield f"event: end\ndata: {json.dumps({'task_id': str(task.id), 'status': payload.get('status'), 'media_file_id': str(media.id) if media else None})}\n\n"
                    break
                yield f"event: progress\ndata: {json.dumps(payload)}\n\n"
        finally:
            await pubsub.unsubscribe(progress_channel(task.id))
            await pubsub.close()
            await r.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/downloads/{task_id}/file", response_model=None)
async def download_file(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> FileResponse | RedirectResponse:
    media = await downloads.resolve_file(session, task_id, user.id)
    if settings.storage_backend == "local":
        local_store = LocalStorageProvider(Path(settings.storage_dir))
        local = await local_store.open_local(media.storage_key)
        if local is None:
            raise AppError("not_found", "文件不存在或已被清理")
        return FileResponse(
            path=local,
            filename=f"{media.id}.{media.format}",
            media_type="application/octet-stream",
        )
    from backend.app.worker.tasks import build_storage

    url = await build_storage().signed_url(media.storage_key)
    return RedirectResponse(url=url, status_code=302)
