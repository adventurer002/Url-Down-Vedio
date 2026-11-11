"""Highlight business contract: real media, isolated DB, no paid providers."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import select, update

from test_tasks import repository, MemoryBus
from test_download import storage
from test_uploads import uploads, ready, media_samples
from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.storage.local import LocalStorageAdapter
from app.main import create_app
from app.modules.ai.providers.base import LLMResult
from app.modules.auth.dependencies import required_user, optional_user
from app.modules.clips.media import FFmpegClipAdapter
from app.modules.clips.models import ClipExport, ClipPlan, ClipTimeline
from app.modules.clips.repository import ClipRepository
from app.modules.clips.router import get_clip_service
from app.modules.clips.schemas import ClipStrategy, ExportQuoteRequest, PlanQuoteRequest
from app.modules.clips.selection import LLMClipSelector, digest, plan_key, units_for
from app.modules.clips.service import ClipService
from app.modules.clips.timing import locate
from app.modules.clips.worker import execute_plan, execute_export
from app.modules.download.router import get_media_service, get_file_task_service
from app.modules.auth.dependencies import resource_user
from app.modules.media.repository import MediaRepository, MediaRecord
from app.modules.media.service import MediaService
from app.modules.tasks.models import Task
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import TaskService
from app.modules.tasks.service import database_call
from app.modules.transcript.data import make_transcript
from app.modules.transcript.models import Transcript


TEXTS = ["缓存键需要包含模型和提示词版本。", "数据库事务保证额度只结算一次。", "清理过期媒体可以释放磁盘空间。"]


class Selector:
    name, model = "fixture", "heuristic-v1"

    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, 0

    async def generate(self, request):
        self.calls += 1
        if self.mode == "timeout":
            raise AppException("LLM_TIMEOUT", "模型超时", 504)
        if request.purpose == "clip_rank":
            content = {"candidate_ids": [x["id"] for x in request.context["candidates"]]}
        else:
            units = request.context["units"]
            content = {"candidates": [] if self.mode == "empty" else [
                {"start_unit": unit["id"] if self.mode != "bad_id" else 9999, "end_unit": unit["id"],
                 "title": f"观点 {unit['id']}", "reason": "独立完整的原文观点", "topic": str(unit["id"]),
                 "completeness": .9, "standalone": .9, "information": .8, "hook": .6} for unit in units[:2]],
                "reason": "没有合适内容" if self.mode == "empty" else ""}
        return LLMResult(content, 10, 20, 1, self.model)


class Timing:
    model, available = "timestamp-fixture", True

    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, 0

    async def transcribe(self, path, language):
        self.calls += 1
        if self.mode == "timeout":
            raise AppException("ASR_TIMEOUT", "定位超时", 504)
        return make_transcript("zh", [{"start": i * 4, "end": i * 4 + 3,
            "text": text if self.mode != "mismatch" else "毫无关系的台词。"} for i, text in enumerate(TEXTS)])


@pytest.fixture(scope="module")
def clip_video(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/ffprobe required")
    path = tmp_path_factory.mktemp("clips-media") / "source.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=25:d=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "libx264", "-g", "250", "-threads", "1",
        "-c:a", "aac", "-shortest", str(path)], timeout=15, check=True)
    return path.read_bytes()


@pytest.fixture
async def clips(uploads, clip_video, monkeypatch):
    u = uploads
    monkeypatch.setattr(settings, "CLIPS_ENABLED", True)
    monkeypatch.setattr(settings, "CLIPS_EXPORT_MAX_TEMP_MB", 16)
    monkeypatch.setattr(settings, "CLIPS_TIMING_MODEL", "timestamp-fixture")
    monkeypatch.setattr(settings, "CLIPS_TIMING_API_BASE_URL", "https://example.com/v1")
    monkeypatch.setattr(settings, "CLIPS_TIMING_API_KEY", SecretStr("fixture-secret"))
    uploaded = await ready(u, clip_video, "source.mp4")
    u.upload_id = uploaded["id"]
    quoted = await u.billing.quote(u.user.id, None, "transcription", None, "zh", upload_id=u.upload_id)
    task = await u.billing.confirm(u.user.id, UUID(quoted["quote_id"]))
    assert u.repository.start(task.id)
    def write(session):
        data = make_transcript("zh", [{"start": i * 4, "end": i * 4 + 3, "text": text} for i, text in enumerate(TEXTS)])
        item = u.transcripts.store(session, task.video_id, task.id, "subtitle", data, "v1", "none")
        return {"transcript_id": str(item.id)}
    done = u.repository.complete_with(task.id, {}, write)
    u.transcript = u.transcripts.get(UUID(done.result_payload["transcript_id"]))
    u.clips = ClipService(ClipRepository(u.repository.sessions), u.transcripts, u.videos, u.tasks,
        u.billing.repository, u.resolver, u.storage, MediaRepository(u.repository.sessions))
    return u


async def plan_task(u, mode="normal", count=2):
    body = PlanQuoteRequest(transcript_id=u.transcript.id, strategy=ClipStrategy(count=count, min_seconds=1, max_seconds=10))
    quote = await u.clips.quote_plan(u.user, body)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    await execute_plan(task.id, u.clips, provider=Selector(mode))
    current = u.repository.get(task.id)
    plan = u.clips.repository.plan(u.user.id, UUID(current.result_payload["clip_plan_id"])) if current.status == "completed" else None
    return current, plan, quote


async def export_task(u, plan, **kwargs):
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[x["id"] for x in plan["candidates"]])
    quote = await u.clips.quote_export(u.user, body)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert (await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))).id == task.id
    await execute_export(task.id, u.clips, provider=kwargs.pop("provider", Timing()), **kwargs)
    return u.repository.get(task.id), quote


async def test_plan_metered_once_references_private_cache_and_timeline(clips):
    u = clips
    before = u.billing.repository.balance(u.user.id)["balances"]
    task, plan, quote = await plan_task(u)
    assert task.status == "completed" and len(plan["candidates"]) == 2
    assert quote["maximum"]["asr_seconds"] == 0 and quote["maximum"]["ai_generations"] == 1
    assert all(x["needs_alignment"] for x in plan["candidates"])
    after = u.billing.repository.balance(u.user.id)["balances"]
    assert after["ai_generations"]["available"] == before["ai_generations"]["available"] - 1
    assert after["asr_seconds"] == before["asr_seconds"]
    again, cached, quote = await plan_task(u)
    assert quote["maximum"]["ai_generations"] == 0 and cached["id"] == plan["id"]
    assert u.billing.repository.balance(u.user.id)["balances"] == after
    with pytest.raises(AppException):
        u.clips.repository.plan(u.other.id, plan["id"])
    modified = SimpleNamespace(**u.transcript.__dict__)
    modified.segments = [{**x, "start": x["start"] + 1, "end": x["end"] + 1} for x in modified.segments]
    assert plan_key(u.user.id, modified, plan["spec"]) != plan["cache_key"]


@pytest.mark.parametrize("mode", ["empty", "bad_id", "timeout"])
async def test_plan_no_delivery_or_failure_releases_all_allowance(clips, mode):
    u = clips
    before = u.billing.repository.balance(u.user.id)["balances"]
    task, plan, _ = await plan_task(u, mode)
    assert task.status == ("completed" if mode == "empty" else "failed")
    if mode == "empty":
        assert plan["outcome"] == "no_highlights" and not plan["candidates"]
        again, _, quote = await plan_task(u, mode)
        assert again.result_payload["cache_hit"] and quote["maximum"]["ai_generations"] == 0
    else:
        assert task.error_code in {"CLIP_SELECTION_INVALID", "LLM_TIMEOUT"}
    assert u.billing.repository.balance(u.user.id)["balances"] == before


async def test_real_export_atomic_files_authorization_reuse_and_no_source_pollution(clips):
    u = clips
    _, plan, _ = await plan_task(u)
    before = u.billing.repository.balance(u.user.id)["balances"]
    timing = Timing()
    task, quote = await export_task(u, plan, provider=timing)
    assert task.status == "completed", (task.error_code, task.error_message)
    assert quote["maximum"]["ai_generations"] == quote["maximum"]["asr_seconds"] == 0
    result = await u.clips.export(u.user, UUID(task.result_payload["clip_export_id"]))
    assert len(result["items"]) == 2 and all(x["download_available"] for x in result["items"])
    assert all(x["verification"]["decoded"] for x in result["items"])
    for item in result["items"]:
        record = u.clips.media.get(UUID(item["file_id"]))
        assert record.purpose == "clip" and record.source_end > record.source_start
        await u.tasks.authorize_file(record.id, record.task_id, u.user)
        with pytest.raises(AppException):
            await u.tasks.authorize_file(record.id, record.task_id, u.other)
    app = create_app()
    app.dependency_overrides[get_media_service] = lambda: MediaService(u.clips.media, u.storage)
    app.dependency_overrides[get_file_task_service] = lambda: u.tasks
    app.dependency_overrides[resource_user] = lambda: u.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        download = await client.get(result["items"][0]["download_url"], headers={"Range": "bytes=0-99"})
        assert download.status_code == 206 and len(download.content) == 100
        app.dependency_overrides[resource_user] = lambda: u.other
        assert (await client.get(result["items"][0]["download_url"])).status_code == 403
    assert u.clips.media.reusable(task.video_id, utcnow()) == []
    assert not (u.storage.root / "tmp" / str(task.id)).exists()
    assert not list((u.storage.root / ".reservations").glob("*.json"))
    after = u.billing.repository.balance(u.user.id)["balances"]
    assert after == before
    again, _ = await export_task(u, plan, provider=Timing("timeout"))
    assert again.status == "completed" and again.result_payload["cache_hit"]
    assert again.result_payload["clip_export_id"] == str(result["id"])
    assert u.billing.repository.balance(u.user.id)["balances"] == after
    await u.service.delete(u.upload_id, u.user.id)
    assert (await u.clips.plan(u.user, plan["id"]))["source_required"]
    # Existing finished clips survive original deletion; cached exports require no new source.
    third, _ = await export_task(u, plan)
    assert third.result_payload["clip_export_id"] == str(result["id"])


@pytest.mark.parametrize("mode", ["success", "timeout", "cancel"])
async def test_filetrans_pipeline_renders_or_cleans_up_real_clip_audio(clips, mode):
    from test_qwen_filetrans import FiletransServer

    u = clips
    _, plan, _ = await plan_task(u)
    server = FiletransServer()
    server.language = None
    server.statuses = ["SUCCEEDED"]
    server.result = {
        "transcripts": [
            {
                "sentences": [
                    {
                        "begin_time": i * 4000,
                        "end_time": i * 4000 + 3000,
                        "text": text,
                        "language": "zh",
                    }
                    for i, text in enumerate(TEXTS)
                ]
            }
        ]
    }
    server.block_poll = mode != "success"
    provider = server.provider(timeout=0.1 if mode == "timeout" else 10)
    before = u.billing.repository.balance(u.user.id)["balances"]
    before_files = u.clips.media.storage_keys()
    operation = asyncio.create_task(export_task(u, plan, provider=provider))
    if mode == "cancel":
        await asyncio.wait_for(server.polling.wait(), 10)
        with u.repository.sessions() as session:
            task_id = session.scalar(
                select(Task.id).where(Task.task_type == "clips_export")
            )
        u.repository.request_cancel(task_id)
    task, _ = await asyncio.wait_for(operation, 20)
    assert (
        task.status
        == {"success": "completed", "timeout": "failed", "cancel": "cancelled"}[mode]
    )
    if mode == "success":
        result = await u.clips.export(
            u.user, UUID(task.result_payload["clip_export_id"])
        )
        assert len(result["items"]) == 2
        assert all(item["verification"]["decoded"] for item in result["items"])
    else:
        assert task.error_code == (
            "ASR_TIMEOUT" if mode == "timeout" else "TASK_CANCELLED"
        )
        assert u.clips.media.storage_keys() == before_files
    assert u.billing.repository.balance(u.user.id)["balances"] == before
    assert not (u.storage.root / "tmp" / str(task.id)).exists()
    assert not list((u.storage.root / ".reservations").glob("*.json"))


@pytest.mark.parametrize("mode", ["mismatch", "timeout", "cancel", "second_render"])
async def test_failed_export_no_partial_files_no_charge_and_source_preserved(clips, mode):
    u = clips
    _, plan, _ = await plan_task(u)
    before = u.billing.repository.balance(u.user.id)["balances"]
    adapter = FFmpegClipAdapter(30, 16 * 1024**2)
    original = adapter.render
    calls = 0
    async def render(*args, **kwargs):
        nonlocal calls
        calls += 1
        if mode == "second_render" and calls == 2:
            raise AppException("CLIP_VALIDATION_FAILED", "第二条失败", 502)
        result = await original(*args, **kwargs)
        if mode == "cancel":
            # Query the running task without relying on arbitrary user-supplied task IDs.
            with u.repository.sessions() as session:
                identifier = session.scalar(select(Task.id).where(Task.task_type == "clips_export", Task.status == "processing"))
            u.repository.request_cancel(identifier)
        return result
    adapter.render = render
    task, _ = await export_task(u, plan, provider=Timing(mode), adapter=adapter)
    assert task.status == ("cancelled" if mode == "cancel" else "failed")
    assert not u.clips.media.storage_keys() and not list((u.storage.root / "files").iterdir())
    assert not (u.storage.root / "tmp" / str(task.id)).exists()
    assert (await u.service.get(u.upload_id, u.user.id))["status"] == "ready"
    assert u.billing.repository.balance(u.user.id)["balances"] == before


async def test_quote_validation_auth_feature_gate_and_no_direct_creation(clips, monkeypatch):
    u = clips
    app = create_app()
    app.dependency_overrides[get_clip_service] = lambda: u.clips
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"transcript_id": str(u.transcript.id), "strategy": {"min_seconds": 1, "max_seconds": 10}}
        assert (await client.post("/api/v1/clips/plans/quotes", json=body)).status_code == 401
        app.dependency_overrides[required_user] = lambda: u.user
        for extra in ({"upload_id": str(u.upload_id)}, {"ffmpeg_args": "-y"}, {"strategy": {"count": 6}},
                      {"strategy": {"min_seconds": 50, "max_seconds": 20}}):
            assert (await client.post("/api/v1/clips/plans/quotes", json={**body, **extra})).status_code == 422
        monkeypatch.setattr(settings, "CLIPS_ENABLED", False)
        response = await client.post("/api/v1/clips/plans/quotes", json=body)
        assert response.status_code == 503 and response.json()["error"]["code"] == "CLIPS_DISABLED"
        monkeypatch.setattr(settings, "CLIPS_ENABLED", True)
        assert (await client.post("/api/v1/clips/plans/quotes", json=body)).status_code == 200
        app.dependency_overrides[required_user] = lambda: u.other
        assert (await client.post("/api/v1/clips/plans/quotes", json=body)).status_code == 403


async def test_deleted_source_can_analyze_but_new_export_requires_same_source(clips):
    u = clips
    await u.service.delete(u.upload_id, u.user.id)
    task, plan, _ = await plan_task(u)
    assert task.status == "completed"
    assert (await u.clips.plan(u.user, plan["id"]))["source_required"]
    with pytest.raises(AppException) as exc:
        await u.clips.quote_export(u.user, ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]]))
    assert exc.value.code == "CLIP_SOURCE_REQUIRED"


async def test_timestamp_change_between_quote_and_confirm_is_rejected(clips):
    u = clips
    quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=u.transcript.id))
    with u.repository.sessions.begin() as session:
        item = session.get(Transcript, u.transcript.id)
        item.segments = [{**x, "start": x["start"] + 1, "end": x["end"] + 1} for x in item.segments]
    with pytest.raises(AppException) as exc:
        await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert exc.value.code == "TRANSCRIPT_SOURCE_MISMATCH"


def test_alignment_rejects_ambiguous_repeated_and_invented_timestamps():
    with pytest.raises(AppException):
        locate("你好世界", [{"start": 0, "end": 2, "text": "你好世界"}, {"start": 3, "end": 5, "text": "你好世界"}], 6)
    with pytest.raises(AppException):
        locate("你好世界", [{"start": 0, "end": 999, "text": "你好世界"}], 6)
    assert locate("你好，世界。", [{"start": 1, "end": 3, "text": "你好世界"}], 6) == (1, 3, 1)


def test_batch_promotion_capacity_manifest_and_uncertain_cleanup(storage):
    identifier, a, b = uuid4(), uuid4(), uuid4()
    with storage.reservation(identifier, 10000):
        directory = storage.create_task_directory(identifier)
        x, y = directory / "x.mp4", directory / "y.mp4"
        x.write_bytes(b"x" * 1000)
        y.write_bytes(b"y" * 2000)
        before = storage.used_bytes() + storage.reserved_bytes()
        keys = storage.promote_batch(identifier, [(x, a, "mp4"), (y, b, "mp4")])
        # The manifest/reservation JSON grows by a few bytes, not by the 3000 moved media bytes.
        assert 0 <= storage.used_bytes() + storage.reserved_bytes() - before < 512
        assert json.loads((directory / ".clip-commit.json").read_text())["files"] == keys
        storage.discard_batch(identifier, {keys[0]})
        assert storage.resolve_file(keys[0]).exists() and not storage.resolve_file(keys[1]).exists()


@pytest.mark.parametrize("variant", ["offset", "nonzero", "vfr", "rotation", "anamorphic"])
async def test_real_media_timebases_seek_and_rotation(storage, tmp_path, variant):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    identifier = uuid4()
    directory = storage.create_task_directory(identifier)
    source = directory / "source.mp4"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=25:d=10"]
    if variant == "offset":
        command += ["-itsoffset", "0.5"]
    command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-g", "250", "-c:a", "aac", "-threads", "1"]
    if variant == "nonzero":
        command += ["-output_ts_offset", "3"]
    if variant == "vfr":
        command += ["-vf", "select=if(lt(t\\,6)\\,not(mod(n\\,2))\\,not(mod(n\\,3)))", "-fps_mode", "vfr"]
    if variant == "anamorphic":
        command += ["-vf", "setsar=2/1"]
    command += [str(source)]
    subprocess.run(command, timeout=15, check=True)
    if variant == "rotation":
        rotated = directory / "rotated.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-display_rotation:v:0", "90",
            "-i", str(source), "-c", "copy", str(rotated)], timeout=10, check=True)
        source = rotated
    adapter = FFmpegClipAdapter(15, 16 * 1024**2)
    controls = {"is_cancel_requested": AsyncMock(return_value=False), "on_progress": AsyncMock(), "check_capacity": storage.check_capacity}
    info = await adapter.inspect(source, directory, **controls)
    start = 0 if variant == "offset" else 4.13
    end = start + 2.31
    result = await adapter.render(source, directory / "output.mp4", info, start, end, **controls)
    assert result["decoded"] and abs(result["duration"] - (end - start)) < .2
    if variant == "offset":
        assert .3 < result["audio_video_offset"] < .7
    if variant == "rotation":
        output = await adapter.inspect(directory / "output.mp4", directory, **controls)
        assert output["height"] > output["width"]
    if variant == "anamorphic":
        output = await adapter.inspect(directory / "output.mp4", directory, **controls)
        assert abs(output["width"] / output["height"] - 3) < .1


async def test_duplicate_active_export_and_daily_render_limit(clips, monkeypatch):
    u = clips
    _, plan, _ = await plan_task(u)
    body = ExportQuoteRequest(plan_id=plan["id"], candidate_ids=[plan["candidates"][0]["id"]])
    quote = await u.clips.quote_export(u.user, body)
    first = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    second_quote = await u.clips.quote_export(u.user, body)
    with pytest.raises(AppException) as exc:
        await u.billing.confirm(u.user.id, UUID(second_quote["quote_id"]))
    assert exc.value.code == "CLIP_EXPORT_IN_PROGRESS"
    assert u.repository.start(first.id)
    u.repository.finish(first.id, "failed", 0, error_code="TEST", error_message="fixture")
    monkeypatch.setattr(settings, "CLIPS_MAX_DAILY_PLAN_EXPORTS", 1)
    with pytest.raises(AppException) as exc:
        await u.clips.quote_export(u.user, body)
    assert exc.value.code == "CLIP_RENDER_LIMIT"


async def test_cancel_at_commit_cleans_promoted_files_and_keeps_timeline(clips, monkeypatch):
    u = clips
    _, plan, _ = await plan_task(u)
    original = u.storage.promote_batch
    def promote(*args, **kwargs):
        keys = original(*args, **kwargs)
        u.repository.request_cancel(args[0])
        return keys
    monkeypatch.setattr(u.storage, "promote_batch", promote)
    task, _ = await export_task(u, plan)
    assert task.status == "cancelled" and not u.clips.media.storage_keys()
    assert not list((u.storage.root / "files").iterdir())
    with u.repository.sessions() as session:
        assert session.scalar(select(ClipTimeline.id)) is not None
        assert session.scalar(select(ClipExport.id)) is None


async def test_export_quota_rejected_before_timing_provider(clips, monkeypatch):
    u = clips
    _, plan, _ = await plan_task(u)
    monkeypatch.setattr(settings, "CLIPS_EXPORT_MAX_TEMP_MB", 10000)
    provider = Timing()
    task, _ = await export_task(u, plan, provider=provider)
    assert task.status == "failed" and task.error_code == "STORAGE_QUOTA_EXCEEDED"
    assert provider.calls == 0


async def test_public_source_downloaded_once_for_multiple_clips(clips, clip_video):
    from app.modules.clips.source import ClipSources
    from app.modules.video.parsers.base import ParsedVideo
    from app.modules.uploads.service import upload_service
    u = clips
    video_id = u.videos.get_or_create(ParsedVideo("youtube", "public-clips", "public fixture", duration=12), "https://example.com/public-clips")
    source = {"video_id": str(video_id), "platform": "youtube", "platform_video_id": "public-clips",
              "url": "https://example.com/public-clips", "title": "public fixture"}
    u.clips.resolver = SimpleNamespace(resolve=AsyncMock(return_value=source))
    quoted = await u.clips.quote_plan(u.user, PlanQuoteRequest(url=source["url"],
        strategy=ClipStrategy(count=2, min_seconds=1, max_seconds=10)))
    task = await u.billing.confirm(u.user.id, UUID(quoted["quote_id"]))
    original = u.transcript
    # An internal pipeline persists its intermediate transcript under the same parent task.
    async def acquire(task_id, on_progress=None):
        return await database_call(
            u.repository.perform_if_active, task_id, lambda session: u.transcripts.store(session, video_id, task_id,
                "subtitle", make_transcript("zh", original.segments), "v1", "none"))
    await execute_plan(task.id, u.clips, provider=Selector(), pipeline=SimpleNamespace(acquire=acquire))
    completed = u.repository.get(task.id)
    assert completed.status == "completed"
    plan = u.clips.repository.plan(u.user.id, UUID(completed.result_payload["clip_plan_id"]))
    calls = []
    async def download(payload, directory, **controls):
        calls.append(payload)
        path = directory / "output.mp4"
        path.write_bytes(clip_video)
        return path
    sources = ClipSources(upload_service(u.repository.sessions, u.storage, u.tasks), u.clips.media, u.storage,
                          SimpleNamespace(download=download))
    exported, _ = await export_task(u, plan, sources=sources)
    assert exported.status == "completed", exported.error_code
    assert len(calls) == 1 and calls[0]["preset_id"] == "video_720_mp4"


async def test_partial_file_promotion_is_recovered_from_manifest(clips, monkeypatch):
    u = clips
    _, plan, _ = await plan_task(u)
    original, calls = u.storage.promote, 0
    def promote(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk commit interruption")
        return original(*args, **kwargs)
    monkeypatch.setattr(u.storage, "promote", promote)
    task, _ = await export_task(u, plan)
    assert task.status == "failed" and calls == 2
    assert not list((u.storage.root / "files").iterdir())
    assert not u.clips.media.storage_keys()


async def test_commit_reply_lost_preserves_already_completed_files(clips, monkeypatch):
    u = clips
    _, plan, _ = await plan_task(u)
    original = u.repository.complete_with
    def commit(*args, **kwargs):
        original(*args, **kwargs)
        raise AppException("DATABASE_UNAVAILABLE", "simulated response lost after commit", 503)
    monkeypatch.setattr(u.repository, "complete_with", commit)
    task, _ = await export_task(u, plan)
    assert task.status == "completed"
    result = await u.clips.export(u.user, UUID(task.result_payload["clip_export_id"]))
    assert len(result["items"]) == 2 and all(x["download_available"] for x in result["items"])


async def test_cleanup_protects_public_source_after_reader_worker_dies(clips, monkeypatch):
    from app.infrastructure.process_runner import ProcessRunner
    from app.infrastructure.storage.cleanup import cleanup_expired_files
    u = clips
    source_task = u.repository.create("simulate_progress", video_id=u.transcript.video_id)
    assert u.repository.start(source_task.id)
    file_id = uuid4()
    key = f"files/{file_id}.mp4"
    u.storage.resolve_file(key).write_bytes(b"source")
    record = MediaRecord(file_id, source_task.id, source_task.video_id, "video_720_mp4", key, "source.mp4",
        "video/mp4", 6, utcnow() - timedelta(hours=1), "full_source")
    u.repository.complete_with(source_task.id, {"file_id": str(file_id)}, lambda session: u.clips.media.register(session, record))
    reader = u.repository.create("simulate_progress")
    directory = u.storage.create_task_directory(reader.id)
    (directory / ".clip-source.json").write_text(json.dumps({"storage_key": key}))
    (directory / ".process.json").write_text(json.dumps({"pgid": 99999999}))
    monkeypatch.setattr(ProcessRunner, "group_exists", staticmethod(lambda pgid: True))
    await cleanup_expired_files(u.storage, u.clips.media, u.repository)
    assert u.storage.resolve_file(key).exists()
    monkeypatch.setattr(ProcessRunner, "group_exists", staticmethod(lambda pgid: False))
    u.repository.request_cancel(reader.id)
    await cleanup_expired_files(u.storage, u.clips.media, u.repository)
    assert not u.storage.resolve_file(key).exists()
