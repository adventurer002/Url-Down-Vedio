from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.response import success_response
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.progress_bus import ProgressBus
from app.infrastructure.queue.redis_client import redis_client
from app.modules.ai.repository import AIRepository
from app.modules.auth.dependencies import required_user
from app.modules.billing.repository import AllowanceRepository
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.service import KnowledgeService
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.service import TaskService, database_call
from app.modules.transcript.repository import TranscriptRepository
from app.modules.video.repository import VideoRepository

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def get_knowledge_service():
    return KnowledgeService(KnowledgeRepository(SessionLocal), TranscriptRepository(SessionLocal), AIRepository(SessionLocal),
        VideoRepository(SessionLocal), TaskService(TaskRepository(SessionLocal), ProgressBus(redis_client)), AllowanceRepository(SessionLocal))


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript_id: UUID
    question: str = Field(min_length=2, max_length=1000)
    language: str = Field(default="zh", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=32)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("问题至少两个字符")
        return value


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcript_id: UUID
    result_id: UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values):
        output = []
        for value in values:
            value = " ".join(value.split())
            if not value or len(value) > 24:
                raise ValueError("标签应为 1–24 个字符")
            if value.casefold() not in {item.casefold() for item in output}:
                output.append(value)
        return output


@router.post("/questions/quotes")
async def quote(body: QuestionRequest, user=Depends(required_user), service=Depends(get_knowledge_service)):
    return success_response(await service.quote(user, body.transcript_id, body.question, body.language.lower()))


@router.get("/answers")
async def answers(transcript_id: UUID, user=Depends(required_user), service=Depends(get_knowledge_service)):
    await service.readable_transcript(transcript_id, user)
    return success_response({"items": await database_call(service.repository.answers, user.id, transcript_id)})


@router.get("/answers/{answer_id}")
async def answer(answer_id: UUID, user=Depends(required_user), service=Depends(get_knowledge_service)):
    return success_response(await database_call(service.repository.answer, user.id, answer_id))


@router.post("/library")
async def save(body: SaveRequest, user=Depends(required_user), service=Depends(get_knowledge_service)):
    return success_response(await service.save(user, body.transcript_id, body.result_id, body.tags))


@router.get("/library")
async def library(q: str = Query("", max_length=200), tag: str = Query("", max_length=24),
    limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0), transcript_id: UUID | None = None,
    user=Depends(required_user), service=Depends(get_knowledge_service)):
    return success_response(await database_call(service.repository.search, user.id, q.strip(), tag.strip(), limit, offset, transcript_id))


@router.get("/library/{item_id}")
async def saved(item_id: UUID, user=Depends(required_user), service=Depends(get_knowledge_service)):
    return success_response(await database_call(service.repository.saved, user.id, item_id))


@router.delete("/library/{item_id}")
async def remove(item_id: UUID, user=Depends(required_user), service=Depends(get_knowledge_service)):
    return success_response(await database_call(service.repository.hide, user.id, item_id))
