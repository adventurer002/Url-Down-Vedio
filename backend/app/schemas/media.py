"""Pydantic v2 request/response schemas for parse + download. Never expose ORM."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ParseRequest(BaseModel):
    url: str = Field(max_length=2048)


class ParseResponse(BaseModel):
    video_id: uuid.UUID


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: str | None = None
    filesize: int | None = None
    vcodec: str | None = None
    acodec: str | None = None


class VideoDetail(BaseModel):
    id: uuid.UUID
    title: str
    uploader: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    platform: str
    webpage_url: str
    formats: list[FormatInfo] = []
    status: str
    error_message: str | None = None


class DownloadRequest(BaseModel):
    video_id: uuid.UUID
    format_id: str = Field(max_length=64)


class DownloadResponse(BaseModel):
    task_id: uuid.UUID


class TaskDetail(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    format_id: str
    quality: str
    status: str
    progress: float
    error_code: str | None = None
    error_message: str | None = None
    media_file_id: uuid.UUID | None = None
    created_at: datetime
    finished_at: datetime | None = None


class Page(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int
