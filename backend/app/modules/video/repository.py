from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.modules.video.models import Video
from app.modules.video.parsers.base import ParsedVideo


class VideoRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

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
