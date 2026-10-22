from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.modules.media.models import MediaFile


@dataclass(frozen=True)
class MediaRecord:
    id: UUID
    task_id: UUID
    video_id: UUID
    preset_id: str
    storage_key: str
    filename: str
    mime_type: str
    size_bytes: int
    expires_at: datetime


class MediaRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def register(self, session: Session, record: MediaRecord) -> None:
        """Participate in the task completion transaction through this contract."""
        session.add(MediaFile(**record.__dict__))
        session.flush()

    def get(self, file_id: UUID) -> MediaRecord:
        with self.sessions() as session:
            item = session.get(MediaFile, file_id)
            if item is None:
                raise AppException("FILE_NOT_FOUND", "文件不存在或已过期", 404)
            return MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})

    def expired(self, now: datetime) -> list[MediaRecord]:
        with self.sessions() as session:
            return [MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})
                    for item in session.scalars(select(MediaFile).where(MediaFile.expires_at <= now))]

    def delete_expired(self, file_id: UUID, now: datetime) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(MediaFile).where(MediaFile.id == file_id, MediaFile.expires_at <= now))

    def storage_keys(self) -> set[str]:
        with self.sessions() as session:
            return set(session.scalars(select(MediaFile.storage_key)))

    def available(self, file_ids: list[UUID], now: datetime) -> list[MediaRecord]:
        with self.sessions() as session:
            return [MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})
                    for item in session.scalars(select(MediaFile).where(MediaFile.id.in_(file_ids), MediaFile.expires_at > now))]

    def reusable(self, video_id: UUID, now: datetime) -> list[MediaRecord]:
        with self.sessions() as session:
            return [MediaRecord(**{key: getattr(item, key) for key in MediaRecord.__dataclass_fields__})
                    for item in session.scalars(select(MediaFile).where(
                        MediaFile.video_id == video_id, MediaFile.expires_at > now
                    ).order_by(MediaFile.created_at.desc()))]
