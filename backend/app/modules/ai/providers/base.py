from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from app.core.exceptions import AppException


@dataclass(frozen=True)
class LLMRequest:
    purpose: Literal["summary", "map", "reduce", "mindmap", "question"]
    context: dict
    schema: dict
    output_language: str
    source_basis: Literal["subtitle", "asr"]


@dataclass(frozen=True)
class LLMResult:
    content: dict
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    model: str


class LLMProvider(Protocol):
    name: str
    model: str
    async def generate(self, request: LLMRequest) -> LLMResult: ...


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    duration_ms: int = 0

    def add(self, reply: LLMResult | "Usage") -> "Usage":
        def total(left, right):
            return left + right if left is not None and right is not None and left + right <= 2147483647 else None
        return Usage(total(self.input_tokens, reply.input_tokens), total(self.output_tokens, reply.output_tokens),
                     self.duration_ms + reply.duration_ms)

    def cost(self, input_rate: Decimal | None, output_rate: Decimal | None, *, free: bool = False) -> Decimal | None:
        if free:
            return Decimal("0.000000")
        if input_rate is None or output_rate is None or self.input_tokens is None or self.output_tokens is None:
            return None
        value = (Decimal(self.input_tokens) * input_rate + Decimal(self.output_tokens) * output_rate) / 1_000_000
        return value.quantize(Decimal("0.000001")) if value < 10000 else None


async def generate(provider: LLMProvider, request: LLMRequest, cancelled: Callable[[], Awaitable[bool]] | None = None) -> LLMResult:
    if cancelled and await cancelled():
        raise AppException("TASK_CANCELLED", "任务已取消", 409)
    operation = asyncio.create_task(provider.generate(request))
    try:
        while not operation.done():
            if cancelled and await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            await asyncio.wait({operation}, timeout=0.5)
        return await operation
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
