"""Local ASR via faster-whisper. No external key needed."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    language: str
    text: str
    segments: list[Segment] = field(default_factory=list)
    duration_seconds: int = 0
    engine: str = ""


class ASRProvider(Protocol):
    async def transcribe(self, audio_path: str) -> TranscriptResult: ...


class ASRError(Exception):
    pass


class FasterWhisperASR:
    """Thin adapter; model weights download on first use (HF cache)."""

    def __init__(self, model_size: str = "small", device: str = "cpu") -> None:
        self._model_size = model_size
        self._device = device
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self._model_size, device=self._device)
        return self._model

    async def transcribe(self, audio_path: str) -> TranscriptResult:
        def _run() -> TranscriptResult:
            try:
                model = self._load()
                segments_iter, info = model.transcribe(audio_path, beam_size=5)
                segments = [
                    Segment(start=s.start, end=s.end, text=s.text.strip())
                    for s in segments_iter
                ]
            except Exception as e:  # noqa: BLE001 - backend errors are heterogeneous
                raise ASRError(f"转写失败: {e}")
            text = "".join(s.text for s in segments).strip()
            duration = int(info.duration) if info.duration else (
                int(segments[-1].end) if segments else 0
            )
            return TranscriptResult(
                language=str(info.language or "unknown"),
                text=text,
                segments=segments,
                duration_seconds=duration,
                engine=f"faster-whisper:{self._model_size}",
            )

        return await asyncio.to_thread(_run)
