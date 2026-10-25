from app.core.exceptions import AppException


class BoundedLLMProvider:
    """An operation budget shared by Map, Reduce and mindmap calls."""
    def __init__(self, provider, limits=None):
        self.provider = provider
        self.name, self.model = provider.name, provider.model
        self.calls, self.output_tokens = 0, 0
        self.limits = limits or {"total_output_tokens": 16384, "output_tokens_per_call": 4096, "max_calls": 16}

    async def generate(self, request):
        remaining = self.limits["total_output_tokens"] - self.output_tokens
        if self.calls >= self.limits["max_calls"] or remaining < 128:
            raise AppException("LLM_BUDGET_EXCEEDED", "单次 AI 操作超过生成规模上限", 422)
        if hasattr(self.provider, "max_tokens"):
            self.provider.max_tokens = min(self.limits["output_tokens_per_call"], remaining)
        self.calls += 1
        result = await self.provider.generate(request)
        self.output_tokens += result.output_tokens if result.output_tokens is not None else self.limits["output_tokens_per_call"]
        if self.output_tokens > self.limits["total_output_tokens"]:
            raise AppException("LLM_BUDGET_EXCEEDED", "模型输出超过本次规模上限", 422)
        return result
