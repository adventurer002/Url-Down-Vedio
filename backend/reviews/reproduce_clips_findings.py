"""Historical audit probes; fixed R1–R3 cases now live in tests/test_clips_r*.py.

Run explicitly (outside normal testpaths):
  PYTHONPATH=.:tests uv run pytest reviews/reproduce_clips_findings.py -q
Only isolated fixtures/fake models are used. No business state or implementation is changed.
"""
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock
import json
import subprocess
import os
import signal
import sys

from pydantic import SecretStr
import pytest

from conftest import isolated_product_settings
from test_clips import (clips, uploads, repository, storage, media_samples, clip_video,
                        Selector, Timing, plan_task, export_task, TEXTS)
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.clips.schemas import ClipStrategy, PlanQuoteRequest, ExportQuoteRequest
from app.modules.clips.timing import locate
from app.modules.clips.worker import execute_plan, prepare_timeline
from app.modules.tasks.repository import utcnow
from app.modules.transcript.data import make_transcript


async def timeline_case(u, monkeypatch, entries, min_seconds=1, max_seconds=90, max_audio=900):
    plan = {"id": uuid4(), "revision": 1, "video_id": u.transcript.video_id, "source_basis": "subtitle",
        "spec": {"strategy": {"min_seconds": min_seconds, "max_seconds": max_seconds}}}
    selected = [{"id": str(i), "start": a, "end": b, "quote": text, "title": str(i)}
                for i, (a, b, text) in enumerate(entries)]
    payload = {"plan_id": str(plan["id"]), "candidate_ids": [x["id"] for x in selected],
        "timing_model": "audit", "timing_base_url": "https://example.com/v1", "allowance": {"clip_limits": {
            "timing_timeout": 10, "max_timing_seconds": max_audio}}}
    task = SimpleNamespace(id=uuid4(), user_id=u.user.id, input_payload=payload)
    monkeypatch.setattr(u.clips.repository, "plan", lambda *args: plan)
    directory = u.storage.create_task_directory(task.id)
    requests = []
    async def audio_window(source, output, info, start, end, directory, **controls):
        output.write_bytes(b"fake-wave")
        requests.append((start, end))
    async def transcribe(path, language):
        start, end = requests[-1]
        return make_transcript("zh", [{"start": max(0, a - start), "end": min(end - start, b - start), "text": text}
                                     for a, b, text in entries if a >= start and b <= end])
    provider = SimpleNamespace(available=True, model="audit", transcribe=transcribe)
    adapter = SimpleNamespace(audio_window=audio_window)
    controls = {"is_cancel_requested": AsyncMock(return_value=False)}
    try:
        await prepare_timeline(task, u.clips, directory / "source.mp4",
            {"sha256": "a" * 64, "origin": 0, "audio_start": 0, "duration": 200}, directory,
            selected, adapter, provider, controls, AsyncMock())
    except AppException as error:
        return error.code, requests
    pytest.fail("Expected the audited defect to be observable")


@pytest.mark.skip(reason="R1 fixed: positive regression in tests/test_clips_r1.py")
async def test_A01_adjacent_nonoverlapping_speech_is_rejected_due_to_padding(clips, monkeypatch):
    code, _ = await timeline_case(clips, monkeypatch, [(0, 40, TEXTS[0]), (40, 80, TEXTS[1])])
    assert code == "CLIP_SELECTION_OVERLAP"


@pytest.mark.skip(reason="R1 fixed: positive regression in tests/test_clips_r1.py")
async def test_A02_exact_duration_limit_is_rejected_due_to_padding(clips, monkeypatch):
    code, _ = await timeline_case(clips, monkeypatch, [(0, 90, TEXTS[0])])
    assert code == "CLIP_DURATION_INVALID"


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
def test_A03_exact_utterance_plus_filler_is_misclassified_as_ambiguous():
    quote = "独立完整观点需要保留必要上下文，避免为了凑时长截断说话内容。" * 4
    with pytest.raises(AppException) as error:
        locate(quote, [{"start": 0, "end": 30, "text": quote}, {"start": 30, "end": 31, "text": "嗯"}], 35)
    assert error.value.code == "CLIP_TIMING_UNRELIABLE"


@pytest.mark.skip(reason="R1 fixed: positive regression in tests/test_clips_r1.py")
async def test_A04_db_read_after_start_strands_running_task_and_held_allowance(clips, monkeypatch):
    u = clips
    quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=u.transcript.id,
        strategy=ClipStrategy(min_seconds=1, max_seconds=10)))
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    original = u.repository.get
    def unavailable(*args):
        raise AppException("DATABASE_UNAVAILABLE", "simulated read failure immediately after start", 503)
    monkeypatch.setattr(u.repository, "get", unavailable)
    with pytest.raises(AppException):
        await execute_plan(task.id, u.clips, provider=Selector())
    monkeypatch.setattr(u.repository, "get", original)
    assert original(task.id).status == "running"
    assert u.billing.repository.balance(u.user.id)["balances"]["ai_generations"]["held"] == 1
    retried = await execute_plan(task.id, u.clips, provider=Selector())
    assert retried["status"] == "running"
    u.repository.request_cancel(task.id)
    assert original(task.id).status == "running" and original(task.id).cancel_requested


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
async def test_A05_late_cache_hit_incorrectly_counts_as_new_render(clips):
    u = clips
    _, plan, _ = await plan_task(u)
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[x["id"] for x in plan["candidates"]])
    old_quote = await u.clips.quote_export(u.user, body)
    task, _ = await export_task(u, plan)
    assert task.status == "completed"
    before = None
    with u.repository.sessions() as session:
        before = u.repository.clip_render_counts(session, u.user.id, str(plan["id"]))
    late = await u.billing.confirm(u.user.id, UUID(old_quote["quote_id"]))
    from app.modules.clips.worker import execute_export
    await execute_export(late.id, u.clips, provider=Timing("timeout"))
    assert u.repository.get(late.id).result_payload["cache_hit"]
    with u.repository.sessions() as session:
        after = u.repository.clip_render_counts(session, u.user.id, str(plan["id"]))
    assert after == (before[0] + 1, before[1] + 1)


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
async def test_A06_old_quote_fails_when_same_source_is_reuploaded_but_old_upload_still_valid(clips, clip_video):
    from test_uploads import ready
    u = clips
    _, plan, _ = await plan_task(u)
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]])
    quote = await u.clips.quote_export(u.user, body)
    new_upload = await ready(u, clip_video, "same-content.mp4")
    assert new_upload["id"] != u.upload_id
    assert (await u.service.get(u.upload_id, u.user.id))["status"] == "ready"
    with pytest.raises(AppException) as error:
        await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert error.value.code == "CLIP_REQUOTE_REQUIRED"


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
async def test_A07_one_corrupt_source_pin_protects_unrelated_media(storage, repository):
    from app.infrastructure.storage.cleanup import clip_source_in_use
    directory = storage.create_task_directory(uuid4())
    (directory / ".clip-source.json").write_text('{"storage_key":')
    assert await clip_source_in_use(storage, repository, f"files/{uuid4()}.mp4")
    assert await clip_source_in_use(storage, repository, f"files/{uuid4()}.mp4")


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
def test_A08_upload_probe_and_clip_probe_disagree_on_unchanged_media_duration(tmp_path):
    from app.modules.clips.media_child import inspect
    from app.modules.uploads.probe import validate_probe
    path = tmp_path / "unequal-tracks.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:d=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-threads", "1",
        "-c:a", "aac", str(path)], check=True, timeout=15)
    raw = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, check=True, timeout=10)
    uploaded = validate_probe(json.loads(raw.stdout), "mp4")["duration_seconds"]
    clip = inspect(path)["duration"]
    assert uploaded == 10 and clip == 5
    assert abs(uploaded - clip) > max(2, uploaded * .01)


@pytest.mark.skip(reason="R1 fixed: positive regression in tests/test_clips_r1.py")
async def test_A09_failed_process_cleanup_still_unlinks_timing_audio(clips, monkeypatch):
    u = clips
    saved = []
    from app.modules.clips.media import FFmpegClipAdapter
    async def failed_window(self, source, output, *args, **kwargs):
        output.write_bytes(b"audio-still-being-written")
        saved.append(output)
        raise AppException("PROCESS_CLEANUP_FAILED", "process group exit unconfirmed", 500)
    # timeline_case supplies a small adapter; replace its construction through the local helper instead.
    plan = {"source_basis": "subtitle", "spec": {"strategy": {"min_seconds": 1, "max_seconds": 90}}}
    monkeypatch.setattr(u.clips.repository, "plan", lambda *args: plan)
    task = SimpleNamespace(id=uuid4(), user_id=u.user.id, input_payload={"plan_id": str(uuid4()),
        "candidate_ids": ["x"], "timing_model": "audit", "timing_base_url": "https://example.com",
        "allowance": {"clip_limits": {"timing_timeout": 10, "max_timing_seconds": 900}}})
    directory = u.storage.create_task_directory(task.id)
    adapter = FFmpegClipAdapter(10, 1024**2)
    monkeypatch.setattr(adapter, "audio_window", failed_window.__get__(adapter))
    with pytest.raises(AppException) as error:
        await prepare_timeline(task, u.clips, directory / "source.mp4",
            {"sha256": "a" * 64, "origin": 0, "audio_start": 0, "duration": 100}, directory,
            [{"id": "x", "start": 0, "end": 30, "quote": TEXTS[0], "title": "x"}], adapter,
            SimpleNamespace(available=True, model="audit"), {"is_cancel_requested": AsyncMock(return_value=False)}, AsyncMock())
    assert error.value.code == "PROCESS_CLEANUP_FAILED" and not saved[0].exists()


@pytest.mark.skip(reason="R1 fixed: positive regression in tests/test_clips_r1.py")
async def test_A10_reconcile_deletes_directory_while_nested_process_group_is_alive(clips):
    from sqlalchemy import update
    from app.modules.tasks.models import Task
    from app.modules.billing.manage import recover
    u = clips
    quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=u.transcript.id))
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert u.repository.start(task.id)
    with u.repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(worker_pid=99999999))
    directory = u.storage.create_task_directory(task.id)
    nested = directory / "asr-chunks-audit"
    nested.mkdir()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        (nested / ".process.json").write_text(json.dumps({"pgid": process.pid}))
        recovered = recover(u.repository, u.storage.root)
        assert recovered == 1 and not directory.exists() and process.poll() is None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
def test_A11_fuzzy_match_accepts_opposite_meaning():
    start, end, score = locate("可以使用缓存", [{"start": 2, "end": 5, "text": "不可以使用缓存"}], 10)
    assert (start, end) == (2, 5) and score > .88


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
async def test_A12_timing_budget_error_is_discovered_after_a_model_request(clips, monkeypatch):
    code, requests = await timeline_case(clips, monkeypatch,
        [(0, 5, TEXTS[0]), (6, 11, TEXTS[1])], max_audio=15)
    assert code == "CLIP_TIMING_BUDGET_EXCEEDED" and len(requests) == 1


@pytest.mark.skip(reason="R3 fixed: positive regressions in tests/test_clips_r3.py")
async def test_A13_exporting_existing_subset_repeats_alignment_and_rendering(clips):
    from app.modules.clips.worker import execute_export
    u = clips
    _, plan, _ = await plan_task(u)
    batch, _ = await export_task(u, plan)
    old = await u.clips.export(u.user, UUID(batch.result_payload["clip_export_id"]))
    quote = await u.clips.quote_export(u.user, ExportQuoteRequest(plan_id=plan["id"],
        candidate_ids=[plan["candidates"][0]["id"]]))
    single = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    provider = Timing()
    original = provider.transcribe
    async def bounded_fixture(path, language):
        import wave
        data = await original(path, language)
        with wave.open(str(path)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        return make_transcript("zh", [x for x in data.segments if x["end"] <= duration])
    provider.transcribe = bounded_fixture
    await execute_export(single.id, u.clips, provider=provider)
    result = await u.clips.export(u.user, UUID(u.repository.get(single.id).result_payload["clip_export_id"]))
    assert provider.calls == 1
    assert result["items"][0]["file_id"] not in {x["file_id"] for x in old["items"]}
    assert any(abs(x["start"] - result["items"][0]["start"]) < .001 and abs(x["end"] - result["items"][0]["end"]) < .001
               for x in old["items"])


@pytest.mark.skip(reason="R2 fixed: positive regressions in tests/test_clips_r2.py")
async def test_A14_valid_low_fps_video_fails_non_frame_aligned_export(storage):
    from app.modules.clips.media import FFmpegClipAdapter
    directory = storage.create_task_directory(uuid4())
    source = directory / "low-fps.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x64:r=1:d=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-threads", "1",
        "-c:a", "aac", str(source)], check=True, timeout=15)
    adapter = FFmpegClipAdapter(15, 16 * 1024**2)
    controls = {"is_cancel_requested": AsyncMock(return_value=False), "on_progress": AsyncMock(),
                "check_capacity": storage.check_capacity}
    info = await adapter.inspect(source, directory, **controls)
    with pytest.raises(AppException) as error:
        await adapter.render(source, directory / "clip.mp4", info, 1.13, 4.44, **controls)
    assert error.value.code == "CLIP_VALIDATION_FAILED"


@pytest.mark.skip(reason="R1 fixed: positive regression in tests/test_clips_r1.py")
async def test_A15_qwen_temporary_directory_also_deletes_after_unconfirmed_exit(clips, monkeypatch):
    from app.infrastructure.process_runner import ProcessRunner
    from app.modules.transcript.providers import QwenASRProvider
    u = clips
    paths = []
    async def interrupted(self, command, directory, **controls):
        (directory / "chunk-00000.wav").write_bytes(b"incomplete-wave")
        paths.append(directory)
        raise AppException("PROCESS_CLEANUP_FAILED", "group exit unconfirmed", 500)
    monkeypatch.setattr(ProcessRunner, "run", interrupted)
    provider = QwenASRProvider("https://example.com/v1", "audit-no-network", "qwen3-asr-flash")
    with pytest.raises(AppException) as error:
        await provider.transcribe(u.service.files.path(u.upload_id), "zh")
    assert error.value.code == "PROCESS_CLEANUP_FAILED" and not paths[0].exists()


def test_C01_control_unique_exact_match_and_actual_disjoint_ambiguity():
    quote = "唯一的完整观点"
    assert locate(quote, [{"start": 1, "end": 3, "text": quote}], 5) == (1, 3, 1)
    with pytest.raises(AppException):
        locate(quote, [{"start": 1, "end": 3, "text": quote}, {"start": 5, "end": 7, "text": quote}], 10)


async def test_C02_control_top_level_live_process_is_preserved(clips):
    from sqlalchemy import update
    from app.modules.tasks.models import Task
    from app.modules.billing.manage import recover
    u = clips
    quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=u.transcript.id))
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert u.repository.start(task.id)
    with u.repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(worker_pid=99999999))
    directory = u.storage.create_task_directory(task.id)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        (directory / ".process.json").write_text(json.dumps({"pgid": process.pid}))
        assert recover(u.repository, u.storage.root) == 0
        assert directory.exists() and process.poll() is None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


async def test_C03_control_valid_source_pin_does_not_protect_unrelated_media(storage, repository):
    from app.infrastructure.storage.cleanup import clip_source_in_use
    task = repository.create("simulate_progress")
    directory = storage.create_task_directory(task.id)
    key = f"files/{uuid4()}.mp4"
    (directory / ".clip-source.json").write_text(json.dumps({"storage_key": key}))
    assert await clip_source_in_use(storage, repository, key)
    assert not await clip_source_in_use(storage, repository, f"files/{uuid4()}.mp4")
