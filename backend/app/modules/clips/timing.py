import asyncio
from difflib import SequenceMatcher
import re
import time
import math
from typing import Protocol

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.transcript.providers import OpenAICompatibleASRProvider


class ClipTimingProvider(Protocol):
    model: str
    available: bool
    async def transcribe(self, path, language): ...


def timing_provider(model=None):
    return OpenAICompatibleASRProvider(settings.CLIPS_TIMING_API_BASE_URL,
        settings.CLIPS_TIMING_API_KEY.get_secret_value(), model or settings.CLIPS_TIMING_MODEL,
        timeout=settings.CLIPS_TIMING_TIMEOUT_SECONDS, max_upload_mb=settings.ASR_MAX_UPLOAD_MB)


def normalize(text):
    # Decimal separators, signed values and technical operators must not collapse to plain words.
    return "".join(re.findall(r"(?<=\d)[.,](?=\d)|[\w+#%°℃−±/<>≤≥=×-]+", text.casefold())).replace("_", "")


def playback_ranges(items, strategy, duration, *, max_total_seconds=None):
    """Keep speech cores intact; optional handles must fit time limits and neighboring cores."""
    ordered = sorted(items, key=lambda item: item["start"])
    automatic = strategy.get("duration_mode", "manual") == "auto"
    # Old plans keep the former batch contract; new quotes freeze the operational budget.
    budget = max_total_seconds if max_total_seconds is not None else (
        settings.CLIPS_EXPORT_MAX_TOTAL_SECONDS if "duration_mode" in strategy else 450)
    for index, item in enumerate(ordered):
        core = item["end"] - item["start"]
        if (not all(math.isfinite(x) for x in (item["start"], item["end"], duration)) or
                item["start"] < 0 or item["end"] > duration or core <= 0):
            raise AppException("CLIP_DURATION_INVALID", "语音核心区间不在有效视频范围内，请选择其他候选", 422)
        if not automatic and not strategy["min_seconds"] - 1e-6 <= core <= strategy["max_seconds"] + 1e-6:
            raise AppException("CLIP_DURATION_INVALID", "语音核心区间不满足已确认时长，请选择其他候选", 422)
        if index and ordered[index - 1]["end"] > item["start"] + 1e-6:
            raise AppException("CLIP_SELECTION_OVERLAP", "语音核心区间重叠，请减少选择", 422)
    remaining = budget - sum(item["end"] - item["start"] for item in ordered)
    if remaining < -1e-6:
        raise AppException("CLIP_EXPORT_BUDGET_EXCEEDED", "整批成片超过处理预算，请减少选择；不会自动截断原句", 422)
    result = {}
    for index, item in enumerate(ordered):
        start, end = item["start"], item["end"]
        left = (ordered[index - 1]["end"] + start) / 2 if index else 0
        right = (end + ordered[index + 1]["start"]) / 2 if index + 1 < len(ordered) else duration
        before, after = min(.1, max(0, start - left)), min(.15, max(0, right - end))
        slack = max(0, remaining if automatic else min(remaining, strategy["max_seconds"] - (end - start)))
        ratio = min(1, slack / (before + after)) if before + after else 0
        remaining -= (before + after) * ratio
        result[item["candidate_id"]] = {**item, "core_start": start, "core_end": end,
            "start": start - before * ratio, "end": end + after * ratio}
    output = [result[item["candidate_id"]] for item in items]
    return output


def critical_tokens(text):
    # Conservative lexical guards; similarity alone cannot establish semantic equivalence.
    return (re.findall(r"不|没|无|未|非|勿|别|禁止|不能|不得|never|not|without", text),
            re.findall(r"[+−±-]?\d+(?:[.,]\d+)*(?:毫秒|秒|分钟|小时|天|次|毫升|升|千克|克|公里|米|ms|mb|gb|kb|hz|kg|cm|mm|s|m|g|[%°℃])?|[零一二三四五六七八九十百千万亿]+", text),
            re.findall(r"[a-z]+[+#]+|[+#%°℃−±/<>≤≥=×-]+", text))


def locate(quote, segments, duration):
    target = normalize(quote)
    if not target or len(target) > 20000 or not segments or len(segments) > 500:
        raise AppException("CLIP_TIMING_UNRELIABLE", "定位文稿不足或超出预算", 422)
    options, comparisons, started = [], 0, time.monotonic()
    texts = [normalize(item["text"]) for item in segments]
    # Enumerate bounded spans first; exact matches never compete with fuzzy extensions.
    spans = []
    for start in range(len(segments)):
        text = ""
        for end in range(start, min(len(segments), start + 120)):
            if time.monotonic() - started > 1:
                raise AppException("CLIP_TIMING_UNRELIABLE", "原文匹配超过计算预算，需要复核", 422)
            text += texts[end]
            if len(text) > len(target) * 1.4:
                break
            if len(text) >= len(target) * .8:
                span = (segments[start]["start"], segments[end]["end"])
                if text == target:
                    options.append((1.0, *span))
                else:
                    spans.append((text, *span))
    if not options:
        for text, start, end in spans:
            comparisons += 1
            if comparisons > 256 or time.monotonic() - started > 1:
                raise AppException("CLIP_TIMING_UNRELIABLE", "原文匹配超过计算预算，需要复核", 422)
            if critical_tokens(text) != critical_tokens(target):
                continue
            score = SequenceMatcher(None, target, text, autojunk=False).ratio()
            if score >= .88:
                options.append((score, start, end))
    options.sort(key=lambda x: (-x[0], x[2] - x[1]))
    if not options:
        raise AppException("CLIP_TIMING_UNRELIABLE", "原文与句级定位不匹配", 422)
    score, start, end = options[0]
    # Nested alternatives describe one occurrence. Disjoint repeated speech remains ambiguous.
    if any(score - other[0] < .015 and min(end, other[2]) <= max(start, other[1])
           for other in options[1:]):
        raise AppException("CLIP_TIMING_UNRELIABLE", "出现重复台词歧义，需要复核", 422)
    if start < 0 or end <= start or end > duration + .1:
        raise AppException("CLIP_TIMING_UNRELIABLE", "定位时间超出音频窗口", 422)
    return start, min(end, duration), score


async def transcribe_window(provider, path, language, cancelled):
    if not provider.available or not provider.model:
        raise AppException("CLIP_TIMING_NOT_CONFIGURED", "请配置支持真实分段时间戳的切片定位模型", 503)
    operation = asyncio.create_task(provider.transcribe(path, language))
    try:
        while not operation.done():
            if await cancelled():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            await asyncio.wait({operation}, timeout=.5)
        return await operation
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
