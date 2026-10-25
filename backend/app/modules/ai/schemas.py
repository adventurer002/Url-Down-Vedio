import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def format_timestamp(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid timestamp")
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


class ChapterSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    timestamp: str = Field(pattern=r"^\d{2,}:[0-5]\d:[0-5]\d$")
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def consistent_timestamp(self):
        if self.timestamp != format_timestamp(self.start_seconds):
            raise ValueError("timestamp does not match start_seconds")
        return self


class SummaryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quote: str = Field(min_length=1, max_length=700)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)


class VideoSummarySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    overview: str = Field(min_length=1, max_length=6000)
    key_points: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=20)
    chapters: list[ChapterSummary] = Field(min_length=1, max_length=1024)
    source_basis: Literal["subtitle", "asr"]
    conclusions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=8)
    audience: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=6)
    action_steps: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=10)
    evidence: list[SummaryEvidence] = Field(default_factory=list, max_length=8)


class MindMapNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=500)
    children: list["MindMapNode"] = Field(default_factory=list, max_length=100)
