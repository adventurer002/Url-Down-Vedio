"""Semantic duration policy, legacy compatibility and bounded real rendering. No paid models."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from test_clips import clips, uploads, repository, storage, media_samples, clip_video
from test_uploads import ready
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.ai.providers.base import LLMResult
from app.modules.clips.schemas import ClipStrategy, PlanQuoteRequest, ExportQuoteRequest
from app.modules.clips.selection import LLMClipSelector, plan_key
from app.modules.clips.service import clip_spec
from app.modules.clips.timing import playback_ranges
from app.modules.clips.worker import execute_plan, execute_export
from app.modules.transcript.data import make_transcript


def test_default_auto_and_legacy_manual_contracts():
    automatic = ClipStrategy()
    assert automatic.duration_mode == "auto"
    assert automatic.min_seconds is automatic.max_seconds is None
    legacy = ClipStrategy(min_seconds=30, max_seconds=60)
    assert legacy.duration_mode == "manual" and legacy.max_seconds == 60
    assert ClipStrategy(min_seconds=20).max_seconds == 90
    assert ClipStrategy(duration_mode="manual", min_seconds=30, max_seconds=240).max_seconds == 240


@pytest.mark.parametrize("value", [
    {"duration_mode": "auto", "min_seconds": 30},
    {"duration_mode": "auto", "max_seconds": 60},
    {"duration_mode": "manual", "min_seconds": 200, "max_seconds": 100},
    {"duration_mode": "manual", "max_seconds": 901},
    {"duration_mode": "auto", "count": 6},
])
def test_ambiguous_or_invalid_strategy_is_rejected(value):
    with pytest.raises(ValidationError):
        ClipStrategy(**value)


class SemanticSelector:
    name, model = "fixture", "heuristic-v1"

    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        if request.purpose == "clip_rank":
            content = {"candidate_ids": [item["id"] for item in request.context["candidates"]]}
        else:
            units = request.context["units"]
            content = {"candidates": [{"start_unit": units[0]["id"], "end_unit": units[-1]["id"],
                "title": "完整的论述", "reason": "包含前提、论证和结论", "topic": "方法",
                "completeness": .95, "standalone": .95, "information": .9, "hook": .8}]}
        return LLMResult(content, 10, 10, 1, self.model)


def semantic_text():
    return make_transcript("zh", [{"start": i * 6, "end": (i + 1) * 6,
        "text": f"这是完整论述的第{i}个步骤。"} for i in range(20)])


@pytest.mark.asyncio
async def test_auto_selects_a_complete_two_minute_argument_and_manual_keeps_its_range():
    data = semantic_text()
    transcript = SimpleNamespace(content_text=data.content_text, segments=data.segments, source_type="subtitle")
    provider = SemanticSelector()
    selector = LLMClipSelector(provider)
    automatic, _, _ = await selector.select(transcript, clip_spec({}, "zh", provider.model), AsyncMock(return_value=False), AsyncMock())
    assert len(automatic) == 1 and automatic[0]["end"] - automatic[0]["start"] == 120
    assert len(automatic[0]["references"]) == 20
    assert "natural semantic boundaries" in provider.requests[0].context["instruction"]
    manual, _, _ = await selector.select(transcript, clip_spec({"min_seconds": 30, "max_seconds": 60}, "zh", provider.model), AsyncMock(return_value=False), AsyncMock())
    assert not manual


def test_auto_and_manual_caches_cannot_collide():
    transcript = SimpleNamespace(id=uuid4(), video_id=uuid4(), content_hash="text", segments=[{"start": 0, "end": 120, "text": "完整内容"}])
    owner = uuid4()
    assert plan_key(owner, transcript, clip_spec({}, "zh", "model")) != plan_key(owner, transcript, clip_spec({"min_seconds": 30, "max_seconds": 60}, "zh", "model"))


@pytest.mark.parametrize("length", [4, 120, 300, 900])
def test_auto_keeps_short_or_long_speech_intact_without_padding_to_a_target(length):
    value = playback_ranges([{"candidate_id": "a", "start": 0, "end": length}], ClipStrategy().model_dump(), length + 10)
    assert value[0]["core_start"] == 0 and value[0]["core_end"] == length
    assert value[0]["end"] - value[0]["start"] >= length
    assert value[0]["end"] <= min(length + .15, 900)


def test_auto_batch_budget_does_not_reintroduce_the_old_450_second_limit():
    strategy = ClipStrategy().model_dump()
    items = [{"candidate_id": "a", "start": 0, "end": 300}, {"candidate_id": "b", "start": 400, "end": 700}]
    assert sum(item["end"] - item["start"] for item in playback_ranges(items, strategy, 800)) > 600
    with pytest.raises(AppException) as error:
        playback_ranges(items, strategy, 800, max_total_seconds=500)
    assert error.value.code == "CLIP_EXPORT_BUDGET_EXCEEDED"
    assert items[0]["end"] == 300 and items[1]["start"] == 400


def test_legacy_ranges_and_overlap_checks_still_apply():
    with pytest.raises(AppException) as error:
        playback_ranges([{"candidate_id": "a", "start": 0, "end": 120}], {"min_seconds": 30, "max_seconds": 60}, 200)
    assert error.value.code == "CLIP_DURATION_INVALID"
    with pytest.raises(AppException) as error:
        playback_ranges([{"candidate_id": "a", "start": 0, "end": 120}, {"candidate_id": "b", "start": 100, "end": 200}], ClipStrategy().model_dump(), 300)
    assert error.value.code == "CLIP_SELECTION_OVERLAP"


@pytest.mark.asyncio
async def test_export_quote_freezes_the_operational_duration_budget(clips, monkeypatch):
    from test_clips import plan_task
    u = clips
    _, plan, _ = await plan_task(u)
    monkeypatch.setattr(settings, "CLIPS_EXPORT_MAX_TOTAL_SECONDS", 720)
    quote = await u.clips.quote_export(u.user, ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]]))
    assert quote["maximum"]["clip_limits"]["max_export_seconds"] == 720
    monkeypatch.setattr(settings, "CLIPS_EXPORT_MAX_TOTAL_SECONDS", 300)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert task.input_payload["allowance"]["clip_limits"]["max_export_seconds"] == 720


@pytest.mark.asyncio
async def test_real_auto_plan_and_render_preserve_a_two_minute_complete_excerpt(clips, tmp_path):
    u = clips
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    source = tmp_path / "long-source.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=10:d=124",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=124", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-c:a", "aac", "-shortest", str(source)], check=True, timeout=25)
    uploaded = await ready(u, source.read_bytes(), "long-source.mp4")
    quote = await u.billing.quote(u.user.id, None, "transcription", None, "zh", upload_id=uploaded["id"])
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert u.repository.start(task.id)
    def write(session):
        transcript = u.transcripts.store(session, task.video_id, task.id, "subtitle", semantic_text(), "v1", "none")
        return {"transcript_id": str(transcript.id)}
    completed = u.repository.complete_with(task.id, {}, write)
    quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=completed.result_payload["transcript_id"]))
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    await execute_plan(task.id, u.clips, provider=SemanticSelector())
    result = u.repository.get(task.id)
    assert result.status == "completed", result.error_code
    plan = await u.clips.plan(u.user, UUID(result.result_payload["clip_plan_id"]))
    assert plan["spec"]["strategy"]["duration_mode"] == "auto"
    assert len(plan["candidates"]) == 1 and plan["candidates"][0]["end"] == 120
    quote = await u.clips.quote_export(u.user, ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]]))
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    class LongTiming:
        model, available = "timestamp-fixture", True
        async def transcribe(self, path, language):
            return semantic_text()
    await execute_export(task.id, u.clips, provider=LongTiming())
    completed = u.repository.get(task.id)
    assert completed.status == "completed", (completed.error_code, completed.error_message)
    delivered = await u.clips.export(u.user, UUID(completed.result_payload["clip_export_id"]))
    item = delivered["items"][0]
    assert item["download_available"] and item["core_end"] - item["core_start"] == 120
    assert 120 <= item["verification"]["duration"] < 121 and item["verification"]["decoded"]
