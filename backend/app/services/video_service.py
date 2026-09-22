"""Parse scheduling + worker-side finish. Route layer only validates + delegates."""

import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import url_safety
from backend.app.core.errors import AppError
from backend.app.db.models import Video
from backend.app.providers.downloader import VideoInfo


def schedule_parse(celery_delay: Callable[[str], str | None], video_id: uuid.UUID) -> None:
    celery_delay(str(video_id))


async def create_parsing_video(
    session: AsyncSession, url: str, user_id: uuid.UUID | None
) -> Video:
    safe = url_safety.validate_url(url)
    video = Video(
        user_id=user_id,
        source_url=safe.url,
        platform="unknown",
        platform_video_id="",
        title=safe.url,
        webpage_url=safe.url,
        status="parsing",
    )
    session.add(video)
    await session.commit()
    await session.refresh(video)
    return video


async def finish_parsing(
    session: AsyncSession, video_id: uuid.UUID, info: VideoInfo
) -> Video:
    video = await session.get(Video, video_id)
    if video is None:
        raise AppError("not_found", "视频不存在")
    video.platform = info.platform
    video.platform_video_id = info.platform_video_id
    video.title = info.title
    video.uploader = info.uploader
    video.thumbnail_url = info.thumbnail
    video.duration_seconds = info.duration_seconds
    video.webpage_url = info.webpage_url or video.source_url
    video.formats = [
        {
            "format_id": f.format_id,
            "ext": f.ext,
            "resolution": f.resolution,
            "filesize": f.filesize,
            "vcodec": f.vcodec,
            "acodec": f.acodec,
        }
        for f in info.formats
    ]
    video.raw_info = info.raw_info
    video.status = "ready"
    await session.commit()
    return video


async def fail_parsing(session: AsyncSession, video_id: uuid.UUID, message: str) -> None:
    video = await session.get(Video, video_id)
    if video is None:
        return
    video.status = "failed"
    video.error_message = message
    await session.commit()
