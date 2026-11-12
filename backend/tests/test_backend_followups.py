"""Regression coverage for cleanup pagination, lock contention and transcript-only AI."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from test_uploads import uploads, repository, storage, media_samples, ready, transcribe
from test_tasks import MemoryBus
from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.infrastructure.storage.retry import wait_for_storage
from app.infrastructure.storage import retry as storage_retry
from app.main import create_app
from app.modules.ai.providers import HeuristicLLMProvider
from app.modules.ai.repository import AIRepository
from app.modules.ai.router import get_ai_service
from app.modules.ai.service import AIService
from app.modules.ai.worker import execute_ai
from app.modules.auth.dependencies import optional_user, required_user
from app.modules.billing.router import get_billing_service
from app.modules.media.repository import MediaRepository
from app.modules.tasks.models import Task, TaskStatus
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import TaskService
from app.modules.transcript.models import Transcript
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.service import TranscriptService, TranscriptionPipeline
from app.modules.uploads.models import Upload
from app.modules.uploads.repository import UploadRepository


@pytest.mark.parametrize("occupied", [False, True])
async def test_cleanup_keyset_scans_past_first_page_even_when_rows_deleted_or_busy(uploads, occupied):
    u = uploads
    identifiers = [uuid4() for _ in range(205)]
    expiry = utcnow() - timedelta(days=2)
    # Identical expiry forces the ID tie-breaker, including page boundaries.
    identifiers.sort()
    with u.repository.sessions.begin() as session:
        for i, identifier in enumerate(identifiers):
            session.add(Upload(id=identifier, user_id=u.user.id, filename="x.mp3", extension="mp3",
                expected_bytes=1, size_bytes=1, status="ready", expires_at=expiry))
            if occupied and i < 100:
                session.add(Task(user_id=u.user.id, task_type="transcription",
                    input_payload={"upload_id": str(identifier)}, expires_at=utcnow() + timedelta(hours=1)))
    target = identifiers[-1]
    u.service.files.directory(target).mkdir()
    u.service.files.path(target).write_bytes(b"x")
    counts = await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository)
    assert counts["uploads"] == (105 if occupied else 205)
    assert not u.service.files.path(target).exists()
    with u.repository.sessions() as session:
        remaining = session.scalar(select(func.count()).select_from(Upload).where(Upload.purged_at.is_(None)))
        assert remaining == (100 if occupied else 0)
    assert (await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository))["uploads"] == 0


async def test_cleanup_rechecks_expiry_after_a_probe_renews_the_source(uploads, monkeypatch):
    u = uploads
    identifier = uuid4()
    with u.repository.sessions.begin() as session:
        session.add(Upload(id=identifier, user_id=u.user.id, filename="x.mp3", extension="mp3",
            expected_bytes=1, size_bytes=1, status="validating", expires_at=utcnow() - timedelta(seconds=1)))
    u.service.files.directory(identifier).mkdir()
    u.service.files.path(identifier).write_bytes(b"x")
    original = UploadRepository.cleanup_candidates
    changed = False
    def page(self, *args):
        nonlocal changed
        items = original(self, *args)
        if items and not changed:
            changed = True
            with self.sessions.begin() as session:
                session.execute(update(Upload).where(Upload.id == identifier).values(status="ready",
                    expires_at=utcnow() + timedelta(days=1)))
        return items
    monkeypatch.setattr(UploadRepository, "cleanup_candidates", page)
    counts = await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository)
    assert counts["uploads"] == 0 and u.service.files.path(identifier).exists()
    assert u.service.repository.get(identifier, u.user.id)["status"] == "ready"


async def test_transcription_waits_for_brief_lease_then_charges_once(uploads, media_samples):
    u = uploads
    item = await ready(u, media_samples["sample.mp3"])
    before = u.billing.repository.balance(u.user.id)["balances"]
    with u.storage.lease():
        operation = asyncio.create_task(transcribe(u, item))
        for _ in range(100):
            if u.tasks.bus.value.get("stage") == "waiting_for_storage":
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("Task did not report storage wait")
        assert not operation.done()
    task, outcome = await asyncio.wait_for(operation, 3)
    assert outcome["status"] == "completed", u.repository.get(task.id)
    after = u.billing.repository.balance(u.user.id)["balances"]
    assert after["asr_seconds"]["available"] < before["asr_seconds"]["available"]
    assert after["asr_seconds"]["held"] == 0 and after["ai_generations"] == before["ai_generations"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_storage_wait_is_bounded_and_cancellable_with_no_charge(uploads, media_samples, monkeypatch, cancel):
    u = uploads
    item = await ready(u, media_samples["sample.mp3"])
    monkeypatch.setattr(storage_retry, "LOCK_WAIT_SECONDS", 5 if cancel else .3)
    waiting = asyncio.Event()
    publish = u.tasks.bus.publish

    async def observe_wait(*args, **kwargs):
        await publish(*args, **kwargs)
        if u.tasks.bus.value.get("stage") == "waiting_for_storage":
            waiting.set()

    monkeypatch.setattr(u.tasks.bus, "publish", observe_wait)
    before = u.billing.repository.balance(u.user.id)["balances"]
    with u.storage.lease():
        operation = asyncio.create_task(transcribe(u, item))
        if cancel:
            await asyncio.wait_for(waiting.wait(), 3)
            u.repository.request_cancel(UUID(u.tasks.bus.value["task_id"]))
        task, outcome = await asyncio.wait_for(operation, 2)
    stored = u.repository.get(task.id)
    assert outcome["status"] == ("cancelled" if cancel else "failed")
    assert stored.error_code == ("TASK_CANCELLED" if cancel else "STORAGE_BUSY")
    assert u.billing.repository.balance(u.user.id)["balances"] == before
    assert not u.storage.task_directory(task.id).exists()


async def test_only_lock_entry_is_retried_never_side_effects_or_quota_errors():
    calls, bodies = [], []
    @contextmanager
    def context():
        calls.append(True)
        if len(calls) < 3:
            raise AppException("STORAGE_BUSY", "busy", 503)
        yield
    with pytest.raises(AppException) as error:
        async with wait_for_storage(context):
            bodies.append(True)
            raise AppException("STORAGE_BUSY", "body failure", 503)
    assert len(calls) == 3 and len(bodies) == 1 and error.value.message == "body failure"
    @contextmanager
    def quota():
        raise AppException("STORAGE_QUOTA_EXCEEDED", "quota", 507)
        yield
    with pytest.raises(AppException) as error:
        async with wait_for_storage(quota):
            pytest.fail("quota cannot succeed")
    assert error.value.code == "STORAGE_QUOTA_EXCEEDED"


async def owned_transcript(u, media_samples):
    item = await ready(u, media_samples["sample.mp3"])
    task, outcome = await transcribe(u, item)
    assert outcome["status"] == "completed"
    identifier = UUID(u.repository.get(task.id).result_payload["transcript_id"])
    return item, identifier


async def transcript_quote(u, identifier, action="ai_summary", user=None):
    owner = user or u.user
    quote = await u.billing.quote(owner.id, None, action, "audio_mp3", "zh", transcript_id=identifier)
    task = await u.billing.confirm(owner.id, UUID(quote["quote_id"]))
    return quote, task


@pytest.mark.parametrize("action", ["ai_summary", "ai_mindmap"])
@pytest.mark.parametrize("remove", ["expired", "deleted"])
async def test_transcript_only_ai_after_source_loss_no_asr_and_free_owned_reuse(uploads, media_samples, monkeypatch, action, remove):
    u = uploads
    item, identifier = await owned_transcript(u, media_samples)
    if remove == "deleted":
        await u.service.delete(item["id"], u.user.id)
    else:
        with u.repository.sessions.begin() as session:
            session.execute(update(Upload).where(Upload.id == item["id"]).values(expires_at=utcnow() - timedelta(seconds=1)))
        await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository)
    assert not u.service.files.path(item["id"]).exists()
    import app.modules.transcript.providers as providers
    monkeypatch.setattr(providers, "get_asr_provider", Mock(side_effect=AssertionError("must never initialize ASR")))
    monkeypatch.setattr(u.resolver, "resolve", AsyncMock(side_effect=AssertionError("must not resolve media")))
    before = u.billing.repository.balance(u.user.id)["balances"]
    quote, task = await transcript_quote(u, identifier, action)
    assert quote["maximum"]["asr_seconds"] == 0 and quote["maximum"]["ai_generations"] == 1
    assert (await execute_ai(task.id, u.tasks, u.results,
        TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), HeuristicLLMProvider()))["status"] == "completed"
    value = u.repository.get(task.id).result_payload
    assert value["transcript_id"] == str(identifier)
    assert u.results.get(UUID(value["result_id"])).transcript_id == identifier
    if action == "ai_mindmap":
        assert value["summary_result_id"]
    after = u.billing.repository.balance(u.user.id)["balances"]
    assert after["asr_seconds"] == before["asr_seconds"]
    assert after["ai_generations"]["available"] == before["ai_generations"]["available"] - 1
    repeated, again = await transcript_quote(u, identifier, action)
    assert repeated["maximum"]["ai_generations"] == repeated["maximum"]["asr_seconds"] == 0
    assert again.input_payload["reuse_result"]["result_id"] == value["result_id"]
    await execute_ai(again.id, u.tasks, u.results,
        TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), HeuristicLLMProvider())
    assert u.billing.repository.balance(u.user.id)["balances"] == after


async def test_transcript_access_rechecked_at_quote_confirm_and_worker(uploads, media_samples, monkeypatch):
    u = uploads
    _, identifier = await owned_transcript(u, media_samples)
    with pytest.raises(AppException) as error:
        await transcript_quote(u, identifier, user=u.other)
    assert error.value.code == "FORBIDDEN"
    quote = await u.billing.quote(u.user.id, None, "ai_summary", "audio_mp3", "zh", transcript_id=identifier)
    with u.repository.sessions.begin() as session:
        granted = session.scalar(select(Task).where(Task.result_payload["transcript_id"].as_string() == str(identifier)))
        grant_id = granted.id
        granted.status = TaskStatus.failed
    with pytest.raises(AppException) as error:
        await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert error.value.code == "FORBIDDEN"
    with u.repository.sessions.begin() as session:
        session.get(Task, grant_id).status = TaskStatus.completed
    task = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    with u.repository.sessions.begin() as session:
        session.get(Task, grant_id).status = TaskStatus.failed
    before = u.billing.repository.balance(u.user.id)["balances"]
    provider = Mock(name="forbidden", model="forbidden", generate=AsyncMock(side_effect=AssertionError("must not call AI")))
    outcome = await execute_ai(task.id, u.tasks, u.results,
        TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), provider)
    assert outcome["status"] == "failed" and u.repository.get(task.id).error_code == "FORBIDDEN"
    assert u.billing.repository.balance(u.user.id)["balances"] == before
    provider.generate.assert_not_awaited()


async def test_transcript_request_contract_auth_and_no_quote_bypass(uploads, media_samples):
    u = uploads
    _, identifier = await owned_transcript(u, media_samples)
    app = create_app()
    app.dependency_overrides[get_billing_service] = lambda: u.billing
    app.dependency_overrides[get_ai_service] = lambda: AIService(u.resolver, u.tasks, u.results)
    app.dependency_overrides[required_user] = lambda: u.user
    app.dependency_overrides[optional_user] = lambda: u.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/billing/quotes", json={"transcript_id": str(identifier), "action": "ai_summary"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["maximum"]["asr_seconds"] == 0
        for action in ("download", "audio_extraction", "transcription"):
            response = await client.post("/api/v1/billing/quotes", json={"transcript_id": str(identifier), "action": action})
            assert response.status_code == 422
        for extra in ({"url": "https://example.com"}, {"upload_id": str(uuid4())}):
            response = await client.post("/api/v1/billing/quotes", json={"transcript_id": str(identifier), "action": "ai_summary", **extra})
            assert response.status_code == 422
        for endpoint in ("/ai/summaries", "/ai/mindmaps"):
            response = await client.post("/api/v1" + endpoint, json={"transcript_id": str(identifier)})
            assert response.status_code == 422 and response.json()["error"]["code"] == "QUOTE_REQUIRED"
        response = await client.post("/api/v1/transcriptions", json={"transcript_id": str(identifier)})
        assert response.status_code == 422


async def test_public_cache_reuse_requires_exact_transcript_not_just_same_text(uploads, monkeypatch):
    from app.modules.ai.providers.base import Usage
    from app.modules.transcript.data import make_transcript
    from app.modules.video.parsers.base import ParsedVideo
    from app.modules.video.repository import VideoRepository
    u = uploads
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", False)
    video_id = VideoRepository(u.repository.sessions).get_or_create(
        ParsedVideo("youtube", "public-transcripts", "public", duration=3600, formats=[]), "https://example.com/public")
    transcripts = []
    for language, start in (("zh", 0), ("en", 100)):
        task = u.repository.create("transcription", video_id, user_id=u.user.id)
        u.repository.start(task.id)
        source = make_transcript(language, [{"start": start, "end": start + 10, "text": "相同文字，不同时间轴。"}])
        def store(session):
            item = u.transcripts.store(session, video_id, task.id, "asr", source, "old-v1", "old-model")
            transcripts.append(item)
            return {"transcript_id": str(item.id)}
        u.repository.complete_with(task.id, {}, store)
    first, second = transcripts
    assert first.content_hash == second.content_hash
    provider = HeuristicLLMProvider()
    from app.modules.ai.summarizer import Summarizer
    generated = await Summarizer(provider).summarize(first, "zh")
    with u.repository.sessions.begin() as session:
        public = u.results.store(session, None, first, "summary", provider.name, provider.model,
            settings.SUMMARY_PROMPT_VERSION, "zh", generated.content.model_dump(), Usage())
    first_scope = u.results.scope_for(first, exact=True)
    second_scope = u.results.scope_for(second, exact=True)
    assert u.results.find_cache(first.content_hash, "summary", provider.model, settings.SUMMARY_PROMPT_VERSION, "zh", first_scope).id == public.id
    assert u.results.find_cache(second.content_hash, "summary", provider.model, settings.SUMMARY_PROMPT_VERSION, "zh", second_scope) is None
    # The old successful delivery remains free when selecting that exact transcript explicitly.
    grant = u.repository.create("ai_summary", video_id, user_id=u.user.id)
    u.repository.start(grant.id)
    u.repository.complete_with(grant.id, {"result_id": str(public.id), "transcript_id": str(first.id)}, lambda session: None)
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", True)
    quote, _ = await transcript_quote(u, first.id)
    assert quote["maximum"]["ai_generations"] == 0
    quote, task = await transcript_quote(u, second.id)
    assert quote["maximum"]["ai_generations"] == 1
    assert task.input_payload["content_version"] == "old-v1"
    assert task.input_payload["duration_seconds"] == 0
    outcome = await execute_ai(task.id, u.tasks, u.results,
        TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), provider)
    assert outcome["status"] == "completed"
    mapped = u.results.get(UUID(u.repository.get(task.id).result_payload["result_id"]))
    assert mapped.transcript_id == second.id and mapped.id != public.id


@pytest.mark.parametrize("failure", ["cancel", "provider"])
async def test_transcript_ai_failure_or_cancel_releases_ai_and_does_not_touch_asr(uploads, media_samples, failure):
    u = uploads
    _, identifier = await owned_transcript(u, media_samples)
    _, task = await transcript_quote(u, identifier)
    before = u.billing.repository.balance(u.user.id)["balances"]
    base = HeuristicLLMProvider()
    async def generate(request):
        if failure == "cancel":
            u.repository.request_cancel(task.id)
            return await base.generate(request)
        raise AppException("LLM_TIMEOUT", "AI 超时", 504)
    provider = SimpleNamespace(name=base.name, model=base.model, generate=generate)
    outcome = await execute_ai(task.id, u.tasks, u.results,
        TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), provider)
    assert outcome["status"] == ("cancelled" if failure == "cancel" else "failed")
    assert u.billing.repository.balance(u.user.id)["balances"] == before


async def test_owned_reuse_still_checks_transcript_at_execution(uploads, media_samples):
    u = uploads
    _, identifier = await owned_transcript(u, media_samples)
    _, first = await transcript_quote(u, identifier)
    pipeline = TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage)
    assert (await execute_ai(first.id, u.tasks, u.results, pipeline, HeuristicLLMProvider()))["status"] == "completed"
    quote, task = await transcript_quote(u, identifier)
    assert quote["maximum"]["ai_generations"] == 0
    # Simulate loss of every grant between confirmation and execution in this disposable database.
    with u.repository.sessions.begin() as session:
        for granted in session.scalars(select(Task).where(Task.id != task.id, Task.status == TaskStatus.completed)):
            granted.status = TaskStatus.failed
    outcome = await execute_ai(task.id, u.tasks, u.results, pipeline, HeuristicLLMProvider())
    assert outcome["status"] == "failed" and u.repository.get(task.id).error_code == "FORBIDDEN"
