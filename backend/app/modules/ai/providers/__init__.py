from app.modules.ai.providers.base import LLMProvider, LLMRequest, LLMResult
from app.modules.ai.providers.heuristic import HeuristicLLMProvider
from app.modules.ai.providers.openai_compatible import OpenAICompatibleLLMProvider
from app.core.config import settings


def get_llm_provider() -> LLMProvider:
    if settings.LLM_PROVIDER == "heuristic" or (settings.LLM_PROVIDER == "auto" and not settings.LLM_API_KEY.get_secret_value()):
        return HeuristicLLMProvider()
    return OpenAICompatibleLLMProvider(settings.LLM_API_BASE_URL, settings.LLM_API_KEY.get_secret_value(),
                                      settings.LLM_MODEL, timeout=settings.LLM_TIMEOUT_SECONDS,
                                      max_tokens=settings.LLM_MAX_OUTPUT_TOKENS)


__all__ = ["LLMProvider", "LLMRequest", "LLMResult", "HeuristicLLMProvider", "OpenAICompatibleLLMProvider", "get_llm_provider"]
