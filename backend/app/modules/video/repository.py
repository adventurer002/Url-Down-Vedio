from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.modules.video.models import Video
from app.modules.video.parsers.base import ParsedVideo


class VideoRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def history_metadata(self, video_ids: list[UUID]) -> dict:
        with self.sessions() as session:
            return {item.id: {"id": str(item.id), "platform": item.platform,
                "platform_video_id": item.platform_video_id, "source_url": item.source_url,
                "title": item.title, "duration": item.duration, "thumbnail": item.thumbnail,
                "uploader": (item.raw_metadata or {}).get("uploader"),
                "subtitle_languages": (item.raw_metadata or {}).get("subtitle_languages", [])}
                for item in session.scalars(select(Video).where(Video.id.in_(video_ids)))}

    def find_by_url(self, source_url: str) -> dict | None:
        with self.sessions() as session:
            item = session.scalar(select(Video).where(Video.source_url == source_url).order_by(Video.created_at))
            if item is None:
                return None
            return {"video_id": str(item.id), "platform": item.platform, "platform_video_id": item.platform_video_id,
                    "title": item.title, "url": source_url}

    def get_or_create(self, video: ParsedVideo, source_url: str) -> UUID:
        with self.sessions.begin() as session:
            identity = (Video.platform == video.platform, Video.platform_video_id == video.platform_video_id)
            existing = session.scalar(select(Video.id).where(*identity))
            if existing:
                return existing
            try:
                with session.begin_nested():
                    item = Video(platform=video.platform, platform_video_id=video.platform_video_id,
                                 source_url=source_url, title=video.title, duration=video.duration,
                                 thumbnail=video.thumbnail, raw_metadata={"uploader": video.uploader,
                                                                          "subtitle_languages": video.subtitle_languages})
                    session.add(item)
                    session.flush()
                    return item.id
            except IntegrityError:
                # The unique index arbitrates simultaneous requests for one video.
                existing = session.scalar(select(Video.id).where(*identity))
                if existing is None:
                    raise
                return existing
