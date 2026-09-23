"""Admin schemas."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    nickname: str
    is_admin: bool
    is_active: bool
    created_at: datetime


class AdminOrderOut(BaseModel):
    order_no: str
    user_id: uuid.UUID
    amount_cents: int
    currency: str
    status: str
    created_at: datetime


class UsageSummaryOut(BaseModel):
    action: str
    calls: int
    llm_input_tokens: int
    llm_output_tokens: int
    asr_seconds: int
    cost_cents: int


class CleanupPreview(BaseModel):
    expired_pending: int


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    action: str
    resource_type: str
    resource_id: str
    created_at: datetime


class PageOut(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int
