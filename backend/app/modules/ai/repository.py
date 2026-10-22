from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.ai.models import AIResult, AIResultType
from app.modules.ai.providers.base import Usage


@dataclass(frozen=True)
class AIRecord:
    id: UUID
    video_id: UUID
    task_id: UUID | None
    transcript_id: UUID
    result_type: str
    source_content_hash: str
    llm_provider: str
    llm_model: str
    prompt_version: str
    output_language: str
    content_json: dict
    markdown_export: str | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    estimated_cost_usd: Decimal | None
    created_at: datetime


def record(item: AIResult) -> AIRecord:
    return AIRecord(**{key: item.result_type.value if key == "result_type" else getattr(item, key) for key in AIRecord.__dataclass_fields__})


class AIRepository:
    def __init__(self, sessions: sessionmaker):
        self.sessions = sessions

    @staticmethod
    def key(content_hash, result_type, model, version, language):
        return (AIResult.source_content_hash == content_hash, AIResult.result_type == AIResultType(result_type),
                AIResult.llm_model == model, AIResult.prompt_version == version, AIResult.output_language == language)

    def find_cache(self, content_hash, result_type, model, version, language) -> AIRecord | None:
        with self.sessions() as session:
            item = session.scalar(select(AIResult).where(*self.key(content_hash, result_type, model, version, language)))
            return record(item) if item else None

    def get(self, result_id: UUID) -> AIRecord:
        with self.sessions() as session:
            item = session.get(AIResult, result_id)
            if item is None:
                raise AppException("AI_RESULT_NOT_FOUND", "AI 结果不存在", 404)
            return record(item)

    def store(self, session: Session, task_id: UUID, transcript, result_type: str, provider: str,
              model: str, version: str, language: str, content: dict, usage: Usage, markdown: str | None = None) -> AIRecord:
        key = self.key(transcript.content_hash, result_type, model, version, language)
        existing = session.scalar(select(AIResult).where(*key))
        if existing:
            return record(existing)
        try:
            with session.begin_nested():
                item = AIResult(id=uuid4(), task_id=task_id, video_id=transcript.video_id, transcript_id=transcript.id,
                                result_type=AIResultType(result_type), source_content_hash=transcript.content_hash,
                                llm_provider=provider, llm_model=model, prompt_version=version, output_language=language,
                                content_json=content, markdown_export=markdown, input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens, duration_ms=usage.duration_ms,
                                estimated_cost_usd=usage.cost(settings.LLM_INPUT_COST_PER_MILLION, settings.LLM_OUTPUT_COST_PER_MILLION,
                                                              free=provider == "heuristic"))
                session.add(item)
                session.flush()
                return record(item)
        except IntegrityError:
            existing = session.scalar(select(AIResult).where(*key))
            if existing is None:
                raise
            return record(existing)
