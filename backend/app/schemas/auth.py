"""Auth request/response schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nickname: str | None = Field(default=None, max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    nickname: str
    is_admin: bool


class MeResponse(BaseModel):
    user: UserOut
    plan_code: str
    plan_name: str
    today_downloads_used: int
    today_downloads_quota: int


class UsageResponse(BaseModel):
    plan_code: str
    today_downloads_used: int
    today_downloads_quota: int
    max_video_duration_seconds: int
    month_llm_input_tokens: int
    month_llm_output_tokens: int
    month_asr_seconds: int
    month_cost_cents: int


class AuthResponse(TokenPair):
    user: UserOut
    registered_at: datetime | None = None
