"""LLM abstraction. First implementation speaks OpenAI-compatible chat API."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@dataclass
class ChatMessage:
    role: str
    content: str


class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult: ...

    @property
    def model_name(self) -> str: ...


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
