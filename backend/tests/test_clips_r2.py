"""R2 positive regressions; real media and isolated persistence, explicit ASR doubles."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from datetime import timedelta

import pytest
from sqlalchemy import update

from test_clips import (clips, uploads, repository, storage, media_samples, clip_video,
                        plan_task, export_task, Timing, TEXTS)
from test_uploads import ready
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.clips.timing import locate
from app.modules.clips.schemas import ExportQuoteRequest
from app.modules.clips.worker import execute_export, prepare_timeline, timing_windows
from app.modules.clips.media import FFmpegClipAdapter
from app.modules.clips.media_child import inspect
from app.modules.media.repository import MediaRecord
from app.modules.tasks.repository import utcnow
from app.modules.transcript.data import make_transcript


def test_exact_quote_with_filler_and_nested_spans_is_one_occurrence():
    quote = "独立完整观点需要保留必要上下文，避免为了凑时长截断说话内容。" * 4
    assert locate(quote, [{"start": 0, "end": 30, "text": quote},
                          {"start": 30, "end": 31, "text": "嗯"}], 35) == (0, 30, 1)
    with pytest.raises(AppException, match="重复台词"):
        locate(quote, [{"start": 0, "end": 30, "text": quote},
                       {"start": 40, "end": 70, "text": quote}], 80)


@pytest.mark.parametrize("quote,actual", [
    ("可以使用缓存", "不可以使用缓存"),
    ("允许删除源文件而不影响结果", "允许删除源文件影响结果"),
    ("每天允许使用100次缓存请求", "每天允许使用101次缓存请求"),
    ("这是一个使用C++进行编译的项目", "这是一个使用C#进行编译的项目"),
    ("阈值超过1.5秒时停止处理", "阈值超过15秒时停止处理"),
    ("温度需要保持在-10度", "温度需要保持在10度"),
    ("最大等待时间为100ms然后退出", "最大等待时间为100s然后退出"),
    ("这一部分的比例应保持1/2", "这一部分的比例应保持12"),
    ("you should always use this cache", "you should not always use this cache"),
])
def test_critical_fuzzy_differences_require_review(quote, actual):
    with pytest.raises(AppException):
        locate(quote, [{"start": 1, "end": 5, "text": actual}], 10)


async def test_late_cache_delivery_does_not_consume_frequency_even_at_limit(clips, monkeypatch):
    u = clips
    monkeypatch.setattr(settings, "CLIPS_MAX_DAILY_EXPORTS", 1)
    _, plan, _ = await plan_task(u)
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[x["id"] for x in plan["candidates"]])
    old_quote = await u.clips.quote_export(u.user, body)
    done, _ = await export_task(u, plan)
    assert done.status == "completed"
    late = await u.billing.confirm(u.user.id, UUID(old_quote["quote_id"]))
    provider = Timing("timeout")
    await execute_export(late.id, u.clips, provider=provider)
    assert u.repository.get(late.id).result_payload["cache_hit"] and provider.calls == 0
    with u.repository.sessions() as session:
        assert u.repository.clip_render_counts(session, u.user.id, plan["id"]) == (1, 1)


async def test_real_work_failure_still_counts_and_cannot_be_relabelled(clips):
    u = clips
    _, plan, _ = await plan_task(u)
    failed, _ = await export_task(u, plan, provider=Timing("timeout"))
    assert failed.status == "failed"
    with u.repository.sessions() as session:
        assert u.repository.clip_render_counts(session, u.user.id, plan["id"]) == (1, 1)


async def test_frozen_upload_survives_same_content_reupload_but_not_deletion(clips, clip_video):
    u = clips
    _, plan, _ = await plan_task(u)
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]])
    quote = await u.clips.quote_export(u.user, body)
    replacement = await ready(u, clip_video, "same.mp4")
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert task.input_payload["upload_id"] == str(u.upload_id)
    await u.clips.revalidate(u.user.id, "clips_export", task.input_payload)
    u.repository.request_cancel(task.id)
    await u.service.delete(u.upload_id, u.user.id)
    with pytest.raises(AppException) as error:
        await u.clips.revalidate(u.user.id, "clips_export", task.input_payload)
    assert error.value.code == "CLIP_REQUOTE_REQUIRED"
    assert (await u.service.get(replacement["id"], u.user.id))["status"] == "ready"


async def test_corrupt_pin_protects_related_source_only(clips):
    from app.infrastructure.storage.cleanup import protected_clip_sources, cleanup_expired_files
    u = clips
    seed = u.repository.create("simulate_progress", video_id=u.transcript.video_id)
    from app.modules.video.parsers.base import ParsedVideo
    unrelated_video = u.videos.get_or_create(ParsedVideo("youtube", "unrelated", "unrelated", duration=10),
                                              "https://example.com/unrelated")
    other = u.repository.create("simulate_progress", video_id=unrelated_video)
    records = []
    for task, video in [(seed, u.transcript.video_id), (other, unrelated_video)]:
        ident = uuid4(); key = f"files/{ident}.mp4"
        u.storage.resolve_file(key).write_bytes(b"source")
        records.append(MediaRecord(ident, task.id, video, "video_720_mp4", key, "source.mp4", "video/mp4", 6,
                                   utcnow() - timedelta(hours=1), "full_source"))
    with u.repository.sessions.begin() as session:
        for record in records:
            u.clips.media.register(session, record)
    reader = u.repository.create("simulate_progress", video_id=u.transcript.video_id)
    directory = u.storage.create_task_directory(reader.id)
    (directory / ".clip-source.json").write_text('{"storage_key":')
    protected = await protected_clip_sources(u.storage, u.repository, u.clips.media)
    assert records[0].storage_key in protected and records[1].storage_key not in protected
    await cleanup_expired_files(u.storage, u.clips.media, u.repository)
    assert u.storage.resolve_file(records[0].storage_key).exists()
    assert not u.storage.resolve_file(records[1].storage_key).exists()


async def test_batch_budget_preflight_makes_zero_provider_requests(clips, monkeypatch):
    u = clips
    plan = {"source_basis": "subtitle", "spec": {"strategy": {"min_seconds": 1, "max_seconds": 90}}}
    monkeypatch.setattr(u.clips.repository, "plan", lambda *args: plan)
    task = SimpleNamespace(id=uuid4(), user_id=u.user.id, input_payload={"plan_id": str(uuid4()),
        "candidate_ids": ["a", "b"], "timing_model": "test", "timing_base_url": "https://example.com",
        "allowance": {"clip_limits": {"timing_timeout": 10, "max_timing_seconds": 15}}})
    directory = u.storage.create_task_directory(task.id)
    adapter = SimpleNamespace(audio_window=AsyncMock())
    provider = SimpleNamespace(available=True, model="test", transcribe=AsyncMock())
    selected = [{"id": ident, "start": a, "end": b, "quote": TEXTS[i], "title": ident}
                for i, (ident, a, b) in enumerate([("a", 0, 5), ("b", 6, 11)])]
    with pytest.raises(AppException) as error:
        await prepare_timeline(task, u.clips, directory / "source.mp4",
            {"sha256": "a" * 64, "origin": 0, "audio_start": 0, "duration": 200}, directory,
            selected, adapter, provider, {"is_cancel_requested": AsyncMock(return_value=False)}, AsyncMock())
    assert error.value.code == "CLIP_TIMING_BUDGET_EXCEEDED"
    adapter.audio_window.assert_not_called(); provider.transcribe.assert_not_called()


def test_windows_merge_and_include_split_overlap_in_budget(monkeypatch):
    monkeypatch.setattr(settings, "ASR_MAX_UPLOAD_MB", 1)
    limits = {"max_timing_seconds": 100, "timing_timeout": 30}
    assert timing_windows([(0, 10), (5, 15)], limits) == [(0, 15)]
    windows = timing_windows([(0, 70)], limits)
    assert len(windows) == 3 and sum(b-a for a,b in windows) == pytest.approx(74)
    assert all((b-a)*32000+4096 <= 1024**2 + .01 for a,b in windows)
    with pytest.raises(AppException):
        timing_windows([(0, 70)], {**limits, "max_timing_seconds": 72})


@pytest.mark.parametrize("fps", [1, 2, 25, 30, "sparse"])
async def test_non_frame_aligned_cut_preserves_held_first_frame(storage, fps):
    directory = storage.create_task_directory(uuid4())
    source = directory / "source.mp4"
    rate = 1 if fps == "sparse" else fps
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"testsrc2=s=64x64:r={rate}:d=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8"]
    if fps == "sparse":
        command += ["-vf", "select=not(eq(n\\,2))", "-fps_mode", "vfr"]
    command += ["-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(source)]
    subprocess.run(command, check=True, timeout=15)
    adapter = FFmpegClipAdapter(15, 16*1024**2)
    controls = {"is_cancel_requested": AsyncMock(return_value=False), "on_progress": AsyncMock(),
                "check_capacity": storage.check_capacity}
    info = await adapter.inspect(source, directory, **controls)
    output = directory / "clip.mp4"
    start = 2.13 if fps == "sparse" else 1.13
    result = await adapter.render(source, output, info, start, 4.44, **controls)
    assert result["decoded"] and abs(result["audio_video_offset"]) < .15
    def frame(path, vf):
        return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", vf,
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True, timeout=10).stdout
    # Select the last actual source frame at/before the cut, independently of renderer filters.
    frames = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(source)],
        capture_output=True, check=True, timeout=10).stdout)["frames"]
    held_index = max(i for i,x in enumerate(frames) if float(x["best_effort_timestamp_time"]) <= start)
    reference = frame(source, f"select=eq(n\\,{held_index})")
    actual = frame(output, "null")
    assert len(actual) == len(reference) == 64*64*3
    assert sum(abs(a-b) for a,b in zip(actual, reference))/len(actual) < 8


async def test_unequal_tracks_use_full_resource_limit_and_common_clip_range(clips, monkeypatch):
    u = clips
    directory = u.storage.create_task_directory(uuid4())
    source = directory / "unequal.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:d=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-threads", "1",
        "-c:a", "aac", str(source)], check=True, timeout=15)
    info = inspect(source)
    assert info["duration"] == 5 and info["resource_duration"] == 10
    assert info["billable_audio_seconds"] == 10
    # Replace fixture source with an actual same-SHA unequal-track upload.
    upload = await ready(u, source.read_bytes(), "unequal.mp4")
    quote = await u.billing.quote(u.user.id, None, "transcription", None, "zh", upload_id=upload["id"])
    trans_task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert u.repository.start(trans_task.id)
    def store(session):
        data = make_transcript("zh", [{"start": i*4, "end": i*4+3, "text": text} for i,text in enumerate(TEXTS[:2])])
        item = u.transcripts.store(session, trans_task.video_id, trans_task.id, "subtitle", data, "v1", "none")
        return {"transcript_id": str(item.id)}
    done = u.repository.complete_with(trans_task.id, {}, store)
    u.transcript = u.transcripts.get(UUID(done.result_payload["transcript_id"]))
    _, plan, _ = await plan_task(u)
    # Only the first 0–3 second candidate is inside the common video/audio range.
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]])
    quote = await u.clips.quote_export(u.user, body)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    from contextlib import asynccontextmanager
    class Source:
        @asynccontextmanager
        async def acquire(self, *args, **kwargs):
            yield source
    await execute_export(task.id, u.clips, sources=Source(), provider=SimpleNamespace(available=True, model="timestamp-fixture",
        transcribe=AsyncMock(return_value=make_transcript("zh", [{"start": 0, "end": 3, "text": TEXTS[0]}]))))
    assert u.repository.get(task.id).status == "completed", u.repository.get(task.id).error_code

    outside = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][1]["id"]])
    quote = await u.clips.quote_export(u.user, outside)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    provider = Timing()
    await execute_export(task.id, u.clips, sources=Source(), provider=provider)
    assert u.repository.get(task.id).error_code == "CLIP_RANGE_UNAVAILABLE" and provider.calls == 0
    # Full-resource rights are checked before ASR, independently of the short playable intersection.
    adapter = FFmpegClipAdapter(15, 16*1024**2)
    adapter.inspect = AsyncMock(return_value={**info, "resource_duration": 100000})
    quote = await u.clips.quote_export(u.user, outside)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    await execute_export(task.id, u.clips, sources=Source(), adapter=adapter, provider=provider)
    assert u.repository.get(task.id).error_code == "CLIP_SOURCE_LIMIT_EXCEEDED" and provider.calls == 0
