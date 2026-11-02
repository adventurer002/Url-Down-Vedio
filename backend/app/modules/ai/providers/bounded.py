import asyncio
from dataclasses import replace

from app.core.exceptions import AppException


class BoundedLLMProvider:
    """An operation budget shared by Map, Reduce and mindmap calls."""
    def __init__(self, provider, limits=None):
        self.provider = provider
        self.name, self.model = provider.name, provider.model
        self.calls, self.output_tokens = 0, 0
        self.reserved_tokens = 0
        self._changed = asyncio.Condition()
        self.limits = limits or {"total_output_tokens": 16384, "output_tokens_per_call": 4096, "max_calls": 16}

    async def generate(self, request):
        async with self._changed:
            while True:
                remaining = self.limits["total_output_tokens"] - self.output_tokens - self.reserved_tokens
                if self.calls >= self.limits["max_calls"]:
                    raise AppException("LLM_BUDGET_EXCEEDED", "单次 AI 操作超过生成规模上限", 422)
                if remaining >= 128:
                    break
                if not self.reserved_tokens:
                    raise AppException("LLM_BUDGET_EXCEEDED", "单次 AI 操作超过生成规模上限", 422)
                await self._changed.wait()
            limit = min(self.limits["output_tokens_per_call"], remaining,
                        getattr(self.provider, "max_tokens", self.limits["output_tokens_per_call"]))
            if request is not None and request.max_output_tokens is not None:
                limit = min(limit, request.max_output_tokens)
            self.reserved_tokens += limit
            self.calls += 1
        # Unknown usage (including cancellation/network failure) conservatively consumes
        # the reservation. Request-local limits never mutate a shared provider.
        charged = limit
        try:
            result = await self.provider.generate(replace(request, max_output_tokens=limit) if request is not None else None)
            charged = result.output_tokens if result.output_tokens is not None else limit
            if charged > limit:
                raise AppException("LLM_BUDGET_EXCEEDED", "模型输出超过本次规模上限", 422)
            return result
        finally:
            async with self._changed:
                self.reserved_tokens -= limit
                self.output_tokens += charged
                self._changed.notify_all()
