from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import math

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.ai.providers.base import LLMProvider, LLMRequest, Usage, generate
from app.modules.ai.schemas import ChapterSummary, VideoSummarySchema, SummaryEvidence, format_timestamp


@dataclass(frozen=True)
class GeneratedSummary:
    content: VideoSummarySchema
    usage: Usage


def split_segments(segments: list[dict], max_chars: int) -> list[list[dict]]:
    if max_chars < 32:
        raise ValueError("chunk budget is too small")
    chunks, current, size = [], [], 0
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        while text:
            room = max_chars - size
            piece = text[:room]
            # Split oversized paragraphs at a sentence/space boundary when possible.
            if len(text) > room:
                boundary = max(piece.rfind(char) for char in ("。", ".", "!", "?", " ", "\n"))
                if boundary >= room // 2:
                    piece = piece[:boundary + 1]
            current.append({"start": float(segment.get("start", 0)), "end": float(segment.get("end", segment.get("start", 0))), "text": piece})
            size += len(piece)
            text = text[len(piece):]
            if size >= max_chars or text:
                chunks.append(current)
                current, size = [], 0
    if current:
        chunks.append(current)
    return chunks


def _text(value, fallback: str, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else fallback[:limit]


def _seconds(value, fallback: float) -> float:
    try:
        if isinstance(value, str) and ":" in value:
            parts = [float(part) for part in value.split(":")]
            if len(parts) not in {2, 3} or any(part < 0 or part >= 60 for part in parts[1:]):
                return fallback
            result = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
        else:
            result = float(value)
        return result if math.isfinite(result) and result >= 0 else fallback
    except (TypeError, ValueError):
        return fallback


def repair_summary(raw: dict, segments: list[dict], source_basis: str) -> VideoSummarySchema:
    if not isinstance(raw, dict) or not segments:
        raise AppException("LLM_INVALID_RESPONSE", "总结缺少有效源文稿", 502)
    first = segments[0]
    minimum = max(0.0, float(first.get("start", 0)))
    maximum = max(minimum, max(float(item.get("end", item.get("start", minimum))) for item in segments))
    excerpt = " ".join(item["text"] for item in segments)[:1200]
    raw_chapters = raw.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raw_chapters = [{"start_seconds": minimum, "title": first["text"][:60], "summary": excerpt}]
    chapters = []
    for index, item in enumerate(raw_chapters[:4]):
        if not isinstance(item, dict):
            continue
        fallback = float(segments[min(index, len(segments) - 1)].get("start", minimum))
        start = _seconds(item.get("start_seconds", item.get("timestamp")), fallback)
        if start < minimum or start > maximum:
            start = fallback
        chapters.append(ChapterSummary(start_seconds=start, timestamp=format_timestamp(start),
                                        title=_text(item.get("title"), first["text"][:60] or f"Chapter {index + 1}", 200),
                                        summary=_text(item.get("summary"), excerpt, 4000)))
    if not chapters:
        chapters = [ChapterSummary(start_seconds=minimum, timestamp=format_timestamp(minimum), title=first["text"][:60] or "Chapter", summary=excerpt)]
    points = raw.get("key_points")
    if isinstance(points, str):
        points = points.splitlines()
    if not isinstance(points, list):
        points = []
    points = list(dict.fromkeys(_text(item, "", 1000) for item in points if isinstance(item, str) and item.strip()))[:20]
    if not points:
        points = [chapter.summary[:1000] for chapter in chapters[:8]]
    def strings(name, count, length):
        values = raw.get(name)
        if not isinstance(values, list):
            return []
        return list(dict.fromkeys(_text(item, "", length) for item in values if isinstance(item, str) and item.strip()))[:count]
    evidence = []
    for item in (raw.get("evidence") if isinstance(raw.get("evidence"), list) else [])[:8]:
        quote = item.get("quote") if isinstance(item, dict) else None
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 700:
            continue
        # The server owns timestamps; invented or paraphrased quotes are discarded.
        matching = next((segment for segment in segments if quote in segment["text"]), None)
        if matching:
            evidence.append(SummaryEvidence(quote=quote, start_seconds=float(matching["start"]),
                end_seconds=float(matching.get("end", matching["start"]))))
    return VideoSummarySchema(overview=_text(raw.get("overview"), excerpt, 6000), key_points=points,
        chapters=sorted(chapters, key=lambda item: item.start_seconds), source_basis=source_basis,
        conclusions=strings("conclusions", 8, 1000), audience=strings("audience", 6, 300),
        action_steps=strings("action_steps", 10, 1000), evidence=evidence)


def _compact(summary: VideoSummarySchema, limit: int) -> dict:
    item = {"start_seconds": summary.chapters[0].start_seconds, "overview": summary.overview,
            "key_points": summary.key_points[:3]}
    while len(json.dumps(item, ensure_ascii=False)) > limit:
        if item["key_points"]:
            item["key_points"].pop()
        else:
            item["overview"] = item["overview"][:max(1, len(item["overview"]) // 2)]
        if len(item["overview"]) == 1 and not item["key_points"]:
            break
    return item


class Summarizer:
    def __init__(self, provider: LLMProvider, chunk_chars: int | None = None, max_chunks: int | None = None):
        self.provider = provider
        self.chunk_chars = chunk_chars or settings.LLM_CHUNK_CHARS
        self.max_chunks = max_chunks or settings.LLM_MAX_CHUNKS

    async def summarize(self, transcript, language: str, *, cancelled=None,
                        on_progress: Callable[[int], Awaitable[None]] | None = None) -> GeneratedSummary:
        segments = transcript.segments or [{"start": 0.0, "end": 0.0, "text": transcript.content_text}]
        chunks = split_segments(segments, self.chunk_chars)
        if not chunks:
            raise AppException("TRANSCRIPT_EMPTY", "没有可用于总结的文稿", 422)
        if len(chunks) > self.max_chunks:
            raise AppException("TRANSCRIPT_TOO_LONG", "文稿超过最大分段数量，请调整服务端分段预算", 413)
        usage, mapped = Usage(), []
        for index, chunk in enumerate(chunks):
            reply = await generate(self.provider, LLMRequest("summary" if len(chunks) == 1 else "map", {"segments": chunk,
                                    "instructions": "提炼核心结论、支撑依据、适合谁、可执行步骤。只填原文能支持的内容；未提及时返回空数组。evidence.quote 必须是逐字原文片段。"},
                                    VideoSummarySchema.model_json_schema(), language, transcript.source_type), cancelled)
            usage = usage.add(reply)
            mapped.append(repair_summary(reply.content, chunk, transcript.source_type))
            if on_progress:
                await on_progress(int((index + 1) / len(chunks) * 75))
        # Keep chapter evidence from every map, even if a reducer omits chapter fields.
        chapters = [chapter for summary in mapped for chapter in summary.chapters]
        level = mapped
        while len(level) > 1:
            compact = [_compact(summary, max(100, self.chunk_chars // 3)) for summary in level]
            groups, group, size = [], [], 0
            for item in compact:
                length = len(json.dumps(item, ensure_ascii=False)) + 2
                if group and size + length > self.chunk_chars:
                    groups.append(group)
                    group, size = [], 0
                group.append(item)
                size += length
            if group:
                groups.append(group)
            if len(groups) >= len(level):
                raise AppException("LLM_CONTEXT_TOO_SMALL", "汇总上下文预算不足", 422)
            reduced = []
            for group in groups:
                reply = await generate(self.provider, LLMRequest("reduce", {"summaries": group,
                                        "instructions": "只汇总原材料支持的结论，不补充常识或虚构行动建议。"},
                                        VideoSummarySchema.model_json_schema(), language, transcript.source_type), cancelled)
                usage = usage.add(reply)
                evidence = [{"start": item["start_seconds"], "end": item["start_seconds"], "text": item["overview"]} for item in group]
                reduced.append(repair_summary(reply.content, evidence, transcript.source_type))
            level = reduced
            if on_progress:
                await on_progress(90)
        enrichments = {key: list(dict.fromkeys(value for item in mapped for value in getattr(item, key)))[:limit]
            for key, limit in (("conclusions", 8), ("audience", 6), ("action_steps", 10))}
        evidence = []
        for item in mapped:
            for citation in item.evidence:
                if citation not in evidence:
                    evidence.append(citation)
        final = level[0].model_copy(update={"chapters": sorted(chapters, key=lambda item: item.start_seconds),
            **enrichments, "evidence": evidence[:8]})
        final = VideoSummarySchema.model_validate(final.model_dump())
        return GeneratedSummary(final, usage)
