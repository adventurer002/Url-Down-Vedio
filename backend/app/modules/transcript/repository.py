from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.modules.transcript.data import TranscriptData
from app.modules.transcript.models import Transcript, TranscriptSource


@dataclass(frozen=True)
class TranscriptRecord:
    id: UUID
    video_id: UUID
    task_id: UUID | None
    source_type: str
    language: str
    content_version: str
    asr_model: str
    content_text: str
    content_hash: str
    segments: list[dict]
    created_at: datetime


def record(item: Transcript) -> TranscriptRecord:
    return TranscriptRecord(**{key: item.source_type.value if key == "source_type" else getattr(item, key)
                               for key in TranscriptRecord.__dataclass_fields__})


class TranscriptRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    def get(self, transcript_id: UUID) -> TranscriptRecord:
        with self.sessions() as session:
            item = session.get(Transcript, transcript_id)
            if item is None:
                raise AppException("TRANSCRIPT_NOT_FOUND", "转写记录不存在", 404)
            return record(item)

    def find_cache(self, video_id: UUID, language: str | None, version: str, asr_model: str) -> TranscriptRecord | None:
        with self.sessions() as session:
            conditions = [Transcript.video_id == video_id, Transcript.content_version == version,
                          Transcript.content_text != "", ((Transcript.source_type == TranscriptSource.subtitle) & (Transcript.asr_model == "none"))
                          | ((Transcript.source_type == TranscriptSource.asr) & (Transcript.asr_model == asr_model))]
            if language:
                conditions.append(Transcript.language == language)
            items = list(session.scalars(select(Transcript).where(*conditions).order_by(Transcript.created_at)))
            items.sort(key=lambda item: item.source_type != TranscriptSource.subtitle)
            return record(items[0]) if items else None

    def store(self, session: Session, video_id: UUID, task_id: UUID, source_type: str, data: TranscriptData,
              version: str, asr_model: str) -> TranscriptRecord:
        key = (Transcript.video_id == video_id, Transcript.source_type == TranscriptSource(source_type),
               Transcript.language == data.language, Transcript.content_version == version, Transcript.asr_model == asr_model)
        existing = session.scalar(select(Transcript).where(*key))
        if existing:
            return record(existing)
        try:
            with session.begin_nested():
                item = Transcript(id=uuid4(), video_id=video_id, task_id=task_id, source_type=TranscriptSource(source_type),
                                  language=data.language, content_version=version, asr_model=asr_model,
                                  content_text=data.content_text, content_hash=data.content_hash, segments=data.segments)
                session.add(item)
                session.flush()
                return record(item)
        except IntegrityError:
            existing = session.scalar(select(Transcript).where(*key))
            if existing is None:
                raise
            return record(existing)
