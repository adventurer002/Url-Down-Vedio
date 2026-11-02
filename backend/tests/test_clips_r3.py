"""Per-candidate alignment and exact-range media reuse, without paid providers."""
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID
import wave

import pytest
from sqlalchemy import delete, func, select, update

from test_clips import (clips, uploads, repository, storage, media_samples, clip_video,
                        plan_task, export_task, Timing, TEXTS)
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.clips.cache import asset_key, candidate_key, window_key
from app.modules.clips.media import FFmpegClipAdapter
from app.modules.clips.models import ClipExport, ClipExportItem, ClipTimeline
from app.modules.clips.schemas import ExportQuoteRequest
from app.modules.clips.worker import execute_export
from app.modules.media.models import MediaFile
from app.modules.tasks.repository import utcnow
from app.modules.transcript.data import make_transcript


class WindowTiming(Timing):
    async def transcribe(self, path, language):
        data = await super().transcribe(path, language)
        with wave.open(str(path)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        return make_transcript("zh", [x for x in data.segments if x["end"] <= duration + .01])


async def subset_quote(u, plan, ids=None):
    return await u.clips.quote_export(u.user, ExportQuoteRequest(plan_id=plan["id"],
        candidate_ids=ids or [plan["candidates"][0]["id"]]))


async def delivered(u, task):
    current = u.repository.get(task.id)
    assert current.status == "completed", (current.error_code, current.error_message)
    return await u.clips.export(u.user, UUID(current.result_payload["clip_export_id"]))


def exports_count(u):
    with u.repository.sessions() as session:
        return session.scalar(select(func.count()).select_from(ClipExport))


async def test_source_free_subset_reuses_file_and_ttl_without_alignment_or_encoding(clips, monkeypatch):
    u = clips
    _, plan, _ = await plan_task(u)
    first, _ = await export_task(u, plan)
    original = await delivered(u, first)
    selected = next(x for x in original["items"] if x["candidate_id"] == plan["candidates"][0]["id"])
    old_record = u.clips.media.get(UUID(selected["file_id"]))
    balance = u.billing.repository.balance(u.user.id)["balances"]
    await u.service.delete(u.upload_id, u.user.id)
    monkeypatch.setattr(settings, "CLIPS_MAX_DAILY_EXPORTS", 1)
    quote = await subset_quote(u, plan)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert task.input_payload["reuse_assets"]["file_ids"] == [selected["file_id"]]
    adapter = FFmpegClipAdapter(15, 16*1024**2)
    adapter.inspect = AsyncMock(side_effect=AssertionError("source not needed"))
    adapter.audio_window = AsyncMock(side_effect=AssertionError("alignment not needed"))
    adapter.render = AsyncMock(side_effect=AssertionError("encoding not needed"))
    provider = Timing("timeout")
    await execute_export(task.id, u.clips, adapter=adapter, provider=provider)
    result = await delivered(u, task)
    assert result["id"] != original["id"]
    assert result["items"][0]["file_id"] == selected["file_id"]
    assert result["items"][0]["expires_at"] == selected["expires_at"]
    assert u.clips.media.get(old_record.id).expires_at == old_record.expires_at
    assert provider.calls == 0
    adapter.inspect.assert_not_called(); adapter.audio_window.assert_not_called(); adapter.render.assert_not_called()
    with u.repository.sessions() as session:
        assert u.repository.clip_render_counts(session, u.user.id, plan["id"]) == (1, 1)
        assert session.scalar(select(func.count()).select_from(MediaFile)) == 2
        assert session.scalar(select(func.count()).select_from(ClipExportItem).where(ClipExportItem.file_id == old_record.id)) == 2
    assert u.billing.repository.balance(u.user.id)["balances"] == balance
    await u.tasks.authorize_file(old_record.id, old_record.task_id, u.user)
    with pytest.raises(AppException):
        await u.tasks.authorize_file(old_record.id, old_record.task_id, u.other)
    assert u.clips.repository.assets_for_keys(u.other.id, [selected["asset_key"]], utcnow())[selected["asset_key"]] == []
    assert u.clips.repository.can_read_file(u.user.id, [old_record.id])
    assert not u.clips.repository.can_read_file(u.other.id, [old_record.id])
    # The new batch itself becomes a normal exact batch cache, with no extra association.
    again = await subset_quote(u, plan)
    second = await u.billing.confirm(u.user.id, UUID(again["quote_id"]))
    await execute_export(second.id, u.clips, provider=provider)
    assert (await delivered(u, second))["id"] == result["id"]


@pytest.mark.parametrize("failure", [None, "render", "commit_expiry"])
async def test_mixed_batch_reuses_valid_asset_and_commits_all_or_nothing(clips, monkeypatch, failure):
    u = clips
    _, plan, _ = await plan_task(u)
    first, _ = await export_task(u, plan)
    original = await delivered(u, first)
    kept, missing = original["items"]
    old_record = u.clips.media.get(UUID(kept["file_id"]))
    u.storage.remove_file(u.clips.media.get(UUID(missing["file_id"])).storage_key)
    provider = Timing("timeout")
    adapter = FFmpegClipAdapter(15, 16*1024**2)
    original_render = adapter.render
    calls = []
    async def render(*args, **kwargs):
        calls.append((args[3], args[4]))
        # A cleanup attempt cannot acquire an exclusive lock on the reused asset.
        if len(calls) == 1:
            try:
                with u.storage.file_lock(u.storage.resolve_file(old_record.storage_key), shared=False):
                    # The reused item might appear later in selection order. Test lock behavior
                    # directly in the commit hook below, when every reused item is held.
                    pass
            except AppException as error:
                assert error.code == "STORAGE_BUSY"
        if failure == "render":
            raise AppException("CLIP_RENDER_FAILED", "injected", 502)
        return await original_render(*args, **kwargs)
    adapter.render = render
    complete = u.repository.complete_with
    def commit(*args, **kwargs):
        with pytest.raises(AppException) as lock_error:
            with u.storage.file_lock(u.storage.resolve_file(old_record.storage_key), shared=False):
                pass
        assert lock_error.value.code == "STORAGE_BUSY"
        if failure == "commit_expiry":
            with u.repository.sessions.begin() as session:
                session.execute(update(MediaFile).where(MediaFile.id == old_record.id).values(expires_at=utcnow()-timedelta(seconds=1)))
        return complete(*args, **kwargs)
    monkeypatch.setattr(u.repository, "complete_with", commit)
    task, _ = await export_task(u, plan, provider=provider, adapter=adapter)
    assert provider.calls == 0 and len(calls) == 1
    if failure:
        assert task.status == "failed"
        assert exports_count(u) == 1
        with u.repository.sessions() as session:
            assert session.scalar(select(func.count()).select_from(MediaFile)) == 2
            assert session.scalar(select(func.count()).select_from(ClipExportItem)) == 2
        assert list((u.storage.root / "files").glob("*.mp4")) == [u.storage.resolve_file(old_record.storage_key)]
    else:
        result = await delivered(u, task)
        reused = next(x for x in result["items"] if x["candidate_id"] == kept["candidate_id"])
        fresh = next(x for x in result["items"] if x["candidate_id"] == missing["candidate_id"])
        assert reused["file_id"] == kept["file_id"] and reused["expires_at"] == kept["expires_at"]
        assert fresh["file_id"] != missing["file_id"]
        assert result["timing_usage"]["audio_seconds"] == 0
        assert exports_count(u) == 2
    assert u.storage.resolve_file(old_record.storage_key).exists()
    assert not (u.storage.root / "tmp" / str(task.id)).exists()


async def test_cached_quote_expiry_requires_requote_without_new_artifacts(clips):
    u = clips
    _, plan, _ = await plan_task(u)
    first, _ = await export_task(u, plan)
    result = await delivered(u, first)
    quote = await subset_quote(u, plan)
    item = next(x for x in result["items"] if x["candidate_id"] == plan["candidates"][0]["id"])
    with u.repository.sessions.begin() as session:
        session.execute(update(MediaFile).where(MediaFile.id == UUID(item["file_id"])).values(expires_at=utcnow()-timedelta(seconds=1)))
    with pytest.raises(AppException) as error:
        await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert error.value.code == "CLIP_REQUOTE_REQUIRED" and exports_count(u) == 1
    old = await u.clips.export(u.user, result["id"])
    assert not next(x for x in old["items"] if x["file_id"] == item["file_id"])["download_available"]


async def test_failed_matching_does_not_poison_window_cache(clips):
    u = clips
    _, plan, _ = await plan_task(u)
    bad = Timing("mismatch")
    failed, _ = await export_task(u, plan, provider=bad)
    assert failed.status == "failed" and bad.calls == 1
    good = Timing()
    completed, _ = await export_task(u, plan, provider=good)
    assert completed.status == "completed" and good.calls == 1


async def test_window_and_asset_cache_reuse_across_owned_plans(clips):
    from app.modules.clips.schemas import PlanQuoteRequest, ClipStrategy
    from app.modules.clips.worker import execute_plan
    from test_clips import Selector
    u = clips
    _, plan, _ = await plan_task(u)
    first, _ = await export_task(u, plan)
    original = await delivered(u, first)
    quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=u.transcript.id,
        strategy=ClipStrategy(count=3, min_seconds=1, max_seconds=9)))
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    await execute_plan(task.id, u.clips, provider=Selector())
    new_plan = u.clips.repository.plan(u.user.id, UUID(u.repository.get(task.id).result_payload["clip_plan_id"]))
    provider = Timing("timeout")
    adapter = FFmpegClipAdapter(15, 16*1024**2)
    adapter.render = AsyncMock(side_effect=AssertionError("same assets should be reused"))
    again, _ = await export_task(u, new_plan, provider=provider, adapter=adapter)
    result = await delivered(u, again)
    assert provider.calls == 0
    assert result["timing_usage"]["audio_seconds"] == 0
    assert {x["file_id"] for x in result["items"]} == {x["file_id"] for x in original["items"]}
    with u.repository.sessions() as session:
        assert u.repository.clip_render_counts(session, u.user.id, new_plan["id"]) == (1, 0)


async def test_removing_adjacent_neighbor_changes_handles_and_requires_new_asset(clips):
    u = clips
    # Construct a genuinely adjacent transcript through the existing transcript repository.
    quote = await u.billing.quote(u.user.id, None, "transcription", None, "zh", upload_id=u.upload_id)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert u.repository.start(task.id)
    def write(session):
        data = make_transcript("zh", [{"start": i*3, "end": i*3+3, "text": text} for i,text in enumerate(TEXTS[:2])])
        item = u.transcripts.store(session, task.video_id, task.id, "subtitle", data, "adjacent", "none")
        return {"transcript_id": str(item.id)}
    done = u.repository.complete_with(task.id, {}, write)
    u.transcript = u.transcripts.get(UUID(done.result_payload["transcript_id"]))
    _, plan, _ = await plan_task(u)
    provider = Timing()
    provider.transcribe = AsyncMock(return_value=make_transcript("zh", [
        {"start": i*3, "end": i*3+3, "text": text} for i,text in enumerate(TEXTS[:2])]))
    first, _ = await export_task(u, plan, provider=provider)
    original = await delivered(u, first)
    first_id = next(x["candidate_id"] for x in original["items"] if x["core_start"] == 0)
    await u.service.delete(u.upload_id, u.user.id)
    with pytest.raises(AppException) as error:
        await subset_quote(u, plan, [first_id])
    assert error.value.code == "CLIP_SOURCE_REQUIRED"
    assert await u.clips.cached_selection(u.user.id, plan, [first_id], "video_720_mp4") is None


async def test_candidate_cache_can_add_missing_neighbor_without_realigning_existing(clips):
    u = clips
    _, plan, _ = await plan_task(u)
    quote = await subset_quote(u, plan)
    first = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    provider = WindowTiming()
    await execute_export(first.id, u.clips, provider=provider)
    original = await delivered(u, first)
    assert provider.calls == 1
    # Candidate two has not been aligned. Only its window is requested; existing candidate stays cached.
    provider = WindowTiming()
    second, _ = await export_task(u, plan, provider=provider)
    result = await delivered(u, second)
    assert provider.calls == 1
    assert original["items"][0]["file_id"] in {x["file_id"] for x in result["items"]}


def test_asset_identity_is_scoped_to_user_bytes_final_range_profile_and_versions(monkeypatch):
    from app.modules.clips import cache
    from uuid import uuid4
    user = uuid4(); other = uuid4()
    info = {"start": 1.1, "end": 4.2}
    key = asset_key(user, "a"*64, info, "video_720_mp4")
    assert key != asset_key(other, "a"*64, info, "video_720_mp4")
    assert key != asset_key(user, "b"*64, info, "video_720_mp4")
    assert key != asset_key(user, "a"*64, {**info, "end": 4.3}, "video_720_mp4")
    assert key != asset_key(user, "a"*64, info, "other_profile")
    monkeypatch.setattr(cache, "RENDERER_VERSION", "next")
    assert key != asset_key(user, "a"*64, info, "video_720_mp4")


@pytest.mark.parametrize("outcome", ["cancel", "reply_lost"])
async def test_cached_delivery_commit_is_atomic_and_idempotent(clips, monkeypatch, outcome):
    u = clips
    _, plan, _ = await plan_task(u)
    first, _ = await export_task(u, plan)
    original = await delivered(u, first)
    quote = await subset_quote(u, plan)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    complete = u.repository.complete_with
    def commit(*args, **kwargs):
        if outcome == "cancel":
            u.repository.request_cancel(task.id)
        value = complete(*args, **kwargs)
        if outcome == "reply_lost":
            raise AppException("DATABASE_UNAVAILABLE", "reply lost after successful commit", 503)
        return value
    monkeypatch.setattr(u.repository, "complete_with", commit)
    await execute_export(task.id, u.clips, provider=Timing("timeout"))
    assert u.repository.get(task.id).status == ("cancelled" if outcome == "cancel" else "completed")
    assert exports_count(u) == (1 if outcome == "cancel" else 2)
    monkeypatch.setattr(u.repository, "complete_with", complete)
    await execute_export(task.id, u.clips, provider=Timing("timeout"))
    assert exports_count(u) == (1 if outcome == "cancel" else 2)
    with u.repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ClipExportItem)) == (2 if outcome == "cancel" else 3)
    for item in original["items"]:
        record = u.clips.media.get(UUID(item["file_id"]))
        with u.storage.file_lock(u.storage.resolve_file(record.storage_key), shared=False):
            pass  # Batch locks were released even when the database reply was lost.


async def test_ttl_cleanup_preserves_shared_batch_history(clips):
    from app.infrastructure.storage.cleanup import cleanup_expired_files
    u = clips
    _, plan, _ = await plan_task(u)
    first, _ = await export_task(u, plan)
    original = await delivered(u, first)
    quote = await subset_quote(u, plan)
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    await execute_export(task.id, u.clips, provider=Timing("timeout"))
    subset = await delivered(u, task)
    file_id = UUID(subset["items"][0]["file_id"])
    with u.repository.sessions.begin() as session:
        session.execute(update(MediaFile).where(MediaFile.id == file_id).values(expires_at=utcnow()-timedelta(seconds=1)))
    await cleanup_expired_files(u.storage, u.clips.media, u.repository)
    for identifier in (original["id"], subset["id"]):
        result = await u.clips.export(u.user, identifier)
        assert not next(x for x in result["items"] if UUID(x["file_id"]) == file_id)["download_available"]
    with u.repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ClipExportItem).where(ClipExportItem.file_id == file_id)) == 2
        assert session.get(MediaFile, file_id) is None


async def test_source_dependency_publication_is_serialized_with_cleanup_snapshot(clips, monkeypatch):
    from app.modules.clips.source import ClipSources
    from app.modules.clips import source as source_module
    from app.modules.media.repository import MediaRecord
    import json
    from uuid import uuid4
    u = clips
    seed = u.repository.create("simulate_progress", video_id=u.transcript.video_id)
    assert u.repository.start(seed.id)
    identifier = uuid4()
    key = f"files/{identifier}.mp4"
    path = u.storage.resolve_file(key)
    path.write_bytes(b"source")
    record = MediaRecord(identifier, seed.id, seed.video_id, "video_720_mp4", key, "source.mp4", "video/mp4", 6,
                         utcnow()+timedelta(hours=1), "full_source")
    u.repository.complete_with(seed.id, {"file_id": str(identifier)}, lambda session: u.clips.media.register(session, record))
    reader = u.repository.create("simulate_progress", video_id=seed.video_id, input_payload={"source_kind": "link"})
    directory = u.storage.create_task_directory(reader.id)
    original = source_module.atomic_json
    called = []
    def publish(*args):
        with pytest.raises(AppException) as lease_error:
            with u.storage.lease():
                pass
        assert lease_error.value.code == "STORAGE_BUSY"
        with pytest.raises(AppException):
            with u.storage.file_lock(path, shared=False):
                pass
        called.append(True)
        original(*args)
    monkeypatch.setattr(source_module, "atomic_json", publish)
    sources = ClipSources(u.service, u.clips.media, u.storage, AsyncMock())
    async with sources.acquire(reader, directory, is_cancel_requested=AsyncMock(return_value=False)) as acquired:
        assert acquired == path and called == [True]
        assert json.loads((directory / ".clip-source.json").read_text())["storage_key"] == key


async def test_cache_loss_after_admission_does_not_start_source_io(clips, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    u = clips
    _, plan, _ = await plan_task(u)
    # Freeze a render quote before another batch creates the single-item cache.
    quote = await subset_quote(u, plan)
    first, _ = await export_task(u, plan)
    assert first.status == "completed"
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    original = u.clips.cached_selection
    calls = []
    async def hit_only_during_admission(*args):
        calls.append(True)
        return await original(*args) if len(calls) == 1 else None
    monkeypatch.setattr(u.clips, "cached_selection", hit_only_during_admission)
    sources = SimpleNamespace(acquire=Mock(side_effect=AssertionError("no source IO after cache-only admission")))
    provider = Timing("timeout")
    await execute_export(task.id, u.clips, sources=sources, provider=provider)
    assert u.repository.get(task.id).error_code == "CLIP_REQUOTE_REQUIRED" and len(calls) == 2
    sources.acquire.assert_not_called()
    assert provider.calls == 0 and exports_count(u) == 1
    with u.repository.sessions() as session:
        assert u.repository.clip_render_counts(session, u.user.id, plan["id"]) == (1, 1)
