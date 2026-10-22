from dataclasses import dataclass
import hashlib
import html
import math
import re
import unicodedata

from app.core.exceptions import AppException


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", html.unescape(text))
    return " ".join(unicodedata.normalize("NFC", text).split())


@dataclass(frozen=True)
class TranscriptData:
    language: str
    content_text: str
    segments: list[dict]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content_text.encode("utf-8")).hexdigest()


def make_transcript(language: str, segments: list[dict]) -> TranscriptData:
    cleaned = []
    for item in segments:
        try:
            start, end = float(item["start"]), float(item["end"])
            text = clean_text(item["text"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AppException("TRANSCRIPT_INVALID", "转写时间轴数据无效", 502) from None
        if text:
            cleaned.append({"start": start, "end": end, "text": text})
    cleaned.sort(key=lambda item: (item["start"], item["end"]))
    merged = []
    for item in cleaned:
        previous = merged[-1] if merged else None
        if previous and item["start"] <= previous["end"] + 0.25:
            if item["text"] == previous["text"]:
                previous["end"] = max(previous["end"], item["end"])
                continue
            if item["text"].startswith(previous["text"]):
                previous.update(text=item["text"], end=max(previous["end"], item["end"]))
                continue
        merged.append(dict(item))
    if not merged:
        raise AppException("TRANSCRIPT_EMPTY", "字幕或转写结果为空", 422)
    return TranscriptData(language, "\n".join(item["text"] for item in merged), merged)
