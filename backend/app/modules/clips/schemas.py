from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.video.sources import AIInputRequest


class ClipStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    count: int = Field(default=3, ge=1, le=5)
    duration_mode: Literal["auto", "manual"] = "auto"
    min_seconds: float | None = Field(default=None, ge=1, le=900)
    max_seconds: float | None = Field(default=None, ge=1, le=900)
    focus: str = Field(default="", max_length=200)

    @model_validator(mode="before")
    @classmethod
    def legacy_range(cls, value):
        # Existing callers specifying seconds retain their explicit manual contract.
        if isinstance(value, dict) and "duration_mode" not in value and any(k in value for k in ("min_seconds", "max_seconds")):
            return {**value, "duration_mode": "manual"}
        return value

    @model_validator(mode="after")
    def ordered(self):
        if self.duration_mode == "auto":
            if self.min_seconds is not None or self.max_seconds is not None:
                raise ValueError("自动切分不接受固定时长范围")
        else:
            self.min_seconds = self.min_seconds if self.min_seconds is not None else 30
            self.max_seconds = self.max_seconds if self.max_seconds is not None else 90
            if self.min_seconds > self.max_seconds:
                raise ValueError("最小时长不能大于最大时长")
        self.focus = " ".join(self.focus.split())
        return self


class PlanQuoteRequest(AIInputRequest):
    strategy: ClipStrategy = Field(default_factory=ClipStrategy)
    language: str = Field(default="zh", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=32)


class ExportQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: UUID
    revision: int = Field(default=1, ge=1)
    candidate_ids: list[str] = Field(min_length=1, max_length=5)
    profile: Literal["video_720_mp4"] = "video_720_mp4"

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.candidate_ids)) != len(self.candidate_ids) or any(len(x) > 64 for x in self.candidate_ids):
            raise ValueError("候选 ID 不可重复且长度不得超过 64")
        return self
