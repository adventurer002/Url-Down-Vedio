"""OpenAI-compatible chat implementation (DeepSeek, OpenAI, ... via config)."""

from typing import Any

import httpx

from backend.app.providers.llm import ChatMessage, LLMError, LLMProvider, LLMResult


class OpenAICompatibleLLM(LLMProvider):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120) -> None:
        if not api_key:
            raise LLMError("LLM API key 未配置", retryable=False)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise LLMError(f"LLM 连接失败: {e}", retryable=True)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise LLMError(f"LLM 服务错误 {resp.status_code}", retryable=True)
        if resp.status_code != 200:
            raise LLMError(
                f"LLM 请求失败 {resp.status_code}: {resp.text[:300]}", retryable=False
            )
        data = resp.json()
        try:
            choice = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            raise LLMError("LLM 返回格式异常", retryable=False)
        return LLMResult(
            text=str(choice),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or self._model),
        )
