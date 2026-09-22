"""Billing schemas: plans, orders, manual grant."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PlanOut(BaseModel):
    code: str
    name: str
    price_cents: int
    currency: str
    duration_days: int | None = None
    features: dict[str, Any]
    max_daily_downloads: int
    max_video_duration_seconds: int
    sort_order: int


class OrderCreate(BaseModel):
    plan_code: str = Field(max_length=32)


class OrderOut(BaseModel):
    order_no: str
    plan_code: str
    amount_cents: int
    currency: str
    status: str
    paid_at: datetime | None = None
    expired_at: datetime | None = None


class GrantRequest(BaseModel):
    email: str = Field(max_length=320)
    plan_code: str = Field(max_length=32)


class GrantResponse(BaseModel):
    user_id: uuid.UUID
    plan_code: str
    subscription_id: uuid.UUID
    expires_at: datetime
