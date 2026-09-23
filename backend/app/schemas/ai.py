"""AI schemas: task status + product outputs."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ProcessingTaskOut(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    task_type: str
    status: str
    output_id: uuid.UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class TriggerResponse(BaseModel):
    task_id: uuid.UUID | None = None
    output_id: uuid.UUID | None = None
    reused: bool = False


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class TranscriptOut(BaseModel):
    id: uuid.UUID
    language: str
    text: str
    segments: list[TranscriptSegment]


class Chapter(BaseModel):
    title: str
    start: int
    end: int


class SummaryOut(BaseModel):
    id: uuid.UUID
    provider: str
    model: str
    summary: str
    key_points: list[str]
    chapters: list[Chapter]
    keywords: list[str]


class MindMapOut(BaseModel):
    id: uuid.UUID
    markdown: str


class UsageEntry(BaseModel):
    action: str
    llm_input_tokens: int
    llm_output_tokens: int
    asr_seconds: int
    storage_bytes: int
    cost_cents: int
    created_at: datetime


class UsageList(BaseModel):
    items: list[UsageEntry]
    total: int
    page: int
    page_size: int


class CostSummary(BaseModel):
    items: list[dict[str, Any]]
