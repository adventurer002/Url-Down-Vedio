from dataclasses import asdict

from app.core.config import settings
from app.modules.ai.providers.base import LLMResult


class CheckpointedLLMProvider:
    def __init__(self, provider, checkpoints, transcript_hash, revisions):
        self.provider = provider
        self.checkpoints = checkpoints
        self.name, self.model = provider.name, provider.model
        self.max_tokens = getattr(provider, "max_tokens", settings.LLM_MAX_OUTPUT_TOKENS)
        self.summary_model = getattr(provider, "summary_model", provider.model)
        self.cost_rates = getattr(provider, "cost_rates", None)
        self.summary_cost_rates = getattr(provider, "summary_cost_rates", self.cost_rates)
        self.scope = [self.name, self.model, self.summary_model, transcript_hash, revisions,
                      getattr(provider, "url", settings.LLM_API_BASE_URL)]

    async def generate(self, request):
        key = self.checkpoints.key("llm-v1", [self.scope, asdict(request)])
        cached = await self.checkpoints.get(key)
        if cached is not None:
            return LLMResult(**{**cached, "duration_ms": 0})
        result = await self.provider.generate(request)
        await self.checkpoints.put(key, asdict(result))
        return result
