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
                "source_kind": item.source_kind, "media_type": item.media_type,
                "uploader": (item.raw_metadata or {}).get("uploader"),
                "subtitle_languages": (item.raw_metadata or {}).get("subtitle_languages", [])}
                for item in session.scalars(select(Video).where(Video.id.in_(video_ids)))}

    def clip_source(self, video_id, user_id):
        from app.core.exceptions import AppException
        with self.sessions() as session:
            item = session.get(Video, video_id)
            if item is None or (item.source_kind == "upload" and item.owner_id != user_id):
                raise AppException("CLIP_SOURCE_NOT_FOUND", "源媒体不存在", 404)
            return {"video_id": str(item.id), "source_kind": item.source_kind, "media_type": item.media_type,
                "content_sha256": item.content_sha256, "duration": item.duration, "url": item.source_url,
                "platform": item.platform, "platform_video_id": item.platform_video_id, "title": item.title}

    def admin_metadata(self, session, video_ids):
        if not video_ids:
            return {}
        fields = (Video.id, Video.platform, Video.platform_video_id, Video.title, Video.duration)
        return {row.id: dict(row._mapping) for row in session.execute(select(*fields).where(Video.id.in_(video_ids)))}

    def find_by_url(self, source_url: str) -> dict | None:
        with self.sessions() as session:
            item = session.scalar(select(Video).where(Video.source_url == source_url, Video.source_kind == "link").order_by(Video.created_at))
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

    def register_upload(self, session, owner_id, content_hash, title, duration, media_type):
        identity = (Video.owner_id == owner_id, Video.content_sha256 == content_hash)
        existing = session.scalar(select(Video.id).where(*identity))
        if existing:
            return existing
        try:
            with session.begin_nested():
                item = Video(source_kind="upload", owner_id=owner_id, content_sha256=content_hash,
                    media_type=media_type, title=title, duration=duration, source_url=None,
                    platform=None, platform_video_id=None, raw_metadata={})
                session.add(item)
                session.flush()
                return item.id
        except IntegrityError:
            existing = session.scalar(select(Video.id).where(*identity))
            if existing is None:
                raise
            return existing

    @staticmethod
    def cache_scope(session, video_id, transcript_id):
        item = session.get(Video, video_id)
        if item and item.source_kind == "upload":
            # A private result must retain this exact transcript and its time axis.
            return f"user:{item.owner_id}:transcript:{transcript_id}"
        return "public"
