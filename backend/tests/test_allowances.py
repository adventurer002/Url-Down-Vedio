"""Allowance lifecycle and API tests. No real provider or payment calls."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
import shutil
import wave

import httpx
import pytest
from sqlalchemy import select, update

from test_tasks import repository, MemoryBus
from test_download import video, storage, FixtureAdapter
from app.core.config import settings
from app.core.exceptions import AppException
from app.main import create_app
from app.modules.auth.models import User
from app.modules.auth.repository import UserRepository
from app.modules.auth.dependencies import required_user, optional_user
from app.modules.billing.models import Allowance, AllowanceAccount, AllowanceReservation
from app.modules.billing.policy import BEIJING, aware, month_boundary, now, seconds
from app.modules.billing.repository import AllowanceRepository
from app.modules.billing.router import get_billing_service
from app.modules.billing.service import BillingService
from app.modules.billing.transfer import transfer_length
from app.modules.tasks.service import TaskService
from app.modules.video.repository import VideoRepository


@pytest.fixture
def billing(repository, video, monkeypatch):
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 64)
    users = UserRepository(repository.sessions)
    user = users.subscription("allowance-test-identity", True, login=True)
    quotas = AllowanceRepository(repository.sessions)
    videos = VideoRepository(repository.sessions)
    identifier = videos.get_or_create(video, "https://example.com/watch")

    def create(action="ai_summary", duration=61, quote_id=None):
        if quote_id is None:
            payload = {"video_id": str(identifier), "url": "https://example.com/watch", "platform": video.platform,
                "platform_video_id": video.platform_video_id, "title": video.title, "duration_seconds": duration,
                "preset_id": "video_720_mp4" if action == "download" else "audio_mp3",
                "output_language": "zh", "language": None, "content_version": settings.TRANSCRIPT_CONTENT_VERSION}
            quote_id = UUID(quotas.save_quote(user.id, action, payload)["quote_id"])
        kind, payload, _ = quotas.quote(user.id, quote_id)
        return repository.create(kind, identifier, payload, now() + timedelta(hours=24), user.id)

    return SimpleNamespace(tasks=repository, quotas=quotas, user=user, users=users,
                           videos=videos, video_id=identifier, create=create)


def test_trial_permanent_and_grants_idempotent(billing):
    b = billing
    assert b.quotas.balance(b.user.id)["balances"]["asr_seconds"]["available"] == 1800
    b.users.subscription("allowance-test-identity", False)
    b.users.subscription("allowance-test-identity", True, login=True)
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 5
    first = b.quotas.grant(b.user.id, "asr_120", "unique-order", "test")
    assert not first["duplicate"]
    assert b.quotas.grant(b.user.id, "asr_120", "unique-order", "test")["duplicate"]
    assert b.quotas.balance(b.user.id)["balances"]["asr_seconds"]["available"] == 9000
    with pytest.raises(AppException, match="同一发放键"):
        b.quotas.grant(b.user.id, "ai_100", "unique-order", "test")


def test_rounding_and_calendar_anchor():
    assert seconds(Decimal("60.001")) == 61
    assert seconds("60") == 60
    for value in [None, 0, -1, "NaN", "Infinity"]:
        with pytest.raises(AppException):
            seconds(value)
    anchor = datetime(2027, 1, 31, 12, tzinfo=BEIJING)
    assert month_boundary(anchor, 1).astimezone(BEIJING).day == 28
    assert month_boundary(anchor, 2).astimezone(BEIJING).day == 31


def test_early_renewal_is_future_allowance(billing):
    b = billing
    first = b.quotas.grant(b.user.id, "monthly", "month-1", "test")
    second = b.quotas.grant(b.user.id, "monthly", "month-2", "test")
    assert aware(second["starts_at"]) == aware(first["expires_at"])
    balance = b.quotas.balance(b.user.id)
    assert balance["member"]
    assert balance["balances"]["asr_seconds"]["available"] == 19800
    assert balance["balances"]["ai_generations"]["available"] == 105


def test_atomic_hold_settlement_and_late_cancel(billing):
    b = billing
    task = b.create()
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"] == {"available": 5, "held": 0}
    assert b.tasks.start(task.id)
    assert not b.tasks.start(task.id)
    assert b.quotas.balance(b.user.id)["balances"]["asr_seconds"] == {"available": 1739, "held": 61}
    b.tasks.record_asr(task.id, 60)
    result = {"result_id": str(uuid4())}
    assert b.tasks.complete_with(task.id, result, lambda session: None).status == "completed"
    b.tasks.request_cancel(task.id)
    b.tasks.finish(task.id, "failed", 0)
    balance = b.quotas.balance(b.user.id)["balances"]
    assert balance["ai_generations"] == {"available": 4, "held": 0}
    assert balance["asr_seconds"] == {"available": 1740, "held": 0}


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "expired"])
def test_failure_releases_once_and_preserves_expiry(billing, terminal):
    b = billing
    task = b.create()
    assert b.tasks.start(task.id)
    with b.tasks.sessions.begin() as session:
        for item in session.scalars(select(Allowance)):
            item.expires_at = now() - timedelta(seconds=1)
    if terminal == "cancelled":
        b.tasks.request_cancel(task.id)
        assert b.tasks.complete_with(task.id, {"result_id": str(uuid4())}, lambda session: pytest.fail("cancelled writer ran")).status == "cancelled"
    else:
        b.tasks.finish(task.id, terminal, 0)
    b.tasks.finish(task.id, terminal, 0)
    balance = b.quotas.balance(b.user.id)["balances"]
    assert balance["asr_seconds"] == {"available": 0, "held": 0}
    assert balance["ai_generations"] == {"available": 0, "held": 0}


def test_result_registration_rollback_keeps_hold(billing):
    b = billing
    task = b.create()
    assert b.tasks.start(task.id)

    def fail(session):
        raise RuntimeError("disk or database write failed")

    with pytest.raises(RuntimeError):
        b.tasks.complete_with(task.id, {"result_id": str(uuid4())}, fail)
    assert b.tasks.get(task.id).status == "running"
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["held"] == 1
    b.tasks.finish(task.id, "failed", 0)
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 5


def test_waiting_does_not_hold_and_addon_wakes_eligibility(billing):
    b = billing
    with b.tasks.sessions.begin() as session:
        b.quotas.lock_account(session, b.user.id)
        for item in session.scalars(select(Allowance)):
            item.used = item.granted
    task = b.create()
    assert not b.tasks.start(task.id)
    assert b.tasks.get(task.id).wait_reason == "allowance"
    b.quotas.grant(b.user.id, "asr_120", "asr-topup", "test")
    b.quotas.grant(b.user.id, "ai_100", "ai-topup", "test")
    assert b.tasks.start(task.id)


def test_free_download_slots_and_success_count(billing):
    b = billing
    first, second, third = [b.create("download") for _ in range(3)]
    assert b.tasks.start(first.id)
    assert not b.tasks.start(second.id)
    assert b.tasks.get(second.id).wait_reason == "capacity"
    b.tasks.complete_with(first.id, {"file_id": str(uuid4())}, lambda session: None)
    assert b.tasks.start(second.id)
    b.tasks.complete_with(second.id, {"file_id": str(uuid4())}, lambda session: None)
    assert not b.tasks.start(third.id)
    assert b.tasks.get(third.id).wait_reason == "daily_downloads"
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 5


def test_member_three_slots_and_expiry_rechecks_queue(billing):
    b = billing
    b.quotas.grant(b.user.id, "monthly", "member", "test")
    tasks = [b.create("download", 1800) for _ in range(4)]
    assert all(b.tasks.start(x.id) for x in tasks[:3])
    assert not b.tasks.start(tasks[3].id)
    with b.tasks.sessions.begin() as session:
        session.get(AllowanceAccount, b.user.id).member_until = now() - timedelta(seconds=1)
    assert b.tasks.complete_with(tasks[0].id, {"file_id": str(uuid4())}, lambda session: None).status == "completed"
    assert not b.tasks.start(tasks[3].id)
    assert b.tasks.get(tasks[3].id).wait_reason == "entitlement"


def test_quote_ownership_confirmation_idempotence_and_limits(billing):
    b = billing
    task = b.create()
    quote_id = UUID(task.input_payload["quote_id"])
    assert b.create(quote_id=quote_id).id == task.id
    with pytest.raises(AppException):
        b.quotas.quote(uuid4(), quote_id)
    with pytest.raises(AppException, match="时长限制"):
        b.create(duration=1201)
    with pytest.raises(AppException, match="先 POST"):
        b.tasks.create("download", b.video_id, {}, user_id=b.user.id)


def test_public_cache_once_per_user_not_once_per_global_result(billing):
    b = billing
    shared_id = str(uuid4())
    for expected in (4, 4):
        task = b.create()
        assert b.tasks.start(task.id)
        b.tasks.complete_with(task.id, {"result_id": shared_id, "cache_hit": True}, lambda session: None)
        assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == expected


def test_owned_bundle_summary_and_audio_file_not_charged_again(billing):
    b = billing
    summary_id, map_id, file_id = [str(uuid4()) for _ in range(3)]
    bundle = b.create("ai_mindmap")
    assert b.tasks.start(bundle.id)
    b.tasks.complete_with(bundle.id, {"result_id": map_id, "summary_result_id": summary_id}, lambda session: None)
    standalone = b.create("ai_summary")
    assert b.tasks.start(standalone.id)
    b.tasks.complete_with(standalone.id, {"result_id": summary_id}, lambda session: None)
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 4
    for _ in range(2):
        task = b.create("audio_extraction")
        assert b.tasks.start(task.id)
        b.tasks.complete_with(task.id, {"file_id": file_id}, lambda session: None)
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 1


@pytest.mark.parametrize("header,expected", [(None, 100), ("bytes=0-9", 10), ("bytes=90-", 10), ("bytes=-10", 10)])
def test_single_range_accounting(header, expected):
    assert transfer_length(100, header) == expected
    assert transfer_length(100, "bytes=0-9", "stale-etag") == 100


def test_traffic_reservation_and_idempotent_release(billing):
    b = billing
    transfer = b.quotas.reserve_transfer(b.user.id, 1024**3)
    with pytest.raises(AppException, match="流量额度不足"):
        b.quotas.reserve_transfer(b.user.id, 1)
    b.quotas.finish_transfer(transfer, 100)
    b.quotas.finish_transfer(transfer, 100)
    daily = b.quotas.balance(b.user.id)["daily"]
    assert daily["egress_used_bytes"] == 100 and daily["egress_held_bytes"] == 0


async def test_api_catalog_quote_confirm_and_auth(billing):
    b = billing
    app = create_app()
    service = BillingService(b.quotas, SimpleNamespace(resolve=AsyncMock(return_value={"video_id": str(b.video_id),
        "url": "https://example.com/watch", "platform": "youtube", "platform_video_id": "fixture"})),
        b.videos, TaskService(b.tasks, MemoryBus(), Mock()))
    app.dependency_overrides[get_billing_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/v1/billing/catalog")).json()["data"]["products"]["monthly"]["price_fen"] == 1990
        assert (await client.get("/api/v1/billing/allowances")).status_code == 401
        app.dependency_overrides[required_user] = lambda: b.user
        quote = await client.post("/api/v1/billing/quotes", json={"url": "https://example.com/watch", "action": "ai_summary"})
        assert quote.status_code == 200
        body = {"quote_id": quote.json()["data"]["quote_id"]}
        first = await client.post("/api/v1/billing/tasks", json=body)
        second = await client.post("/api/v1/billing/tasks", json=body)
        assert first.status_code == second.status_code == 202
        assert first.json()["data"]["task_id"] == second.json()["data"]["task_id"]
        assert (await client.post("/api/v1/billing/grant", json={})).status_code == 404


async def test_download_range_counts_bytes_not_downloads(billing, storage):
    from app.modules.download.worker import execute_download
    from app.modules.download.router import get_media_service
    from app.modules.media.repository import MediaRepository
    from app.modules.media.service import MediaService
    b = billing
    task = b.create("download", 1)
    await execute_download(task.id, TaskService(b.tasks, MemoryBus()), MediaRepository(b.tasks.sessions), storage, FixtureAdapter())
    completed = b.tasks.get(task.id)
    assert completed.status == "completed"
    app = create_app()
    app.dependency_overrides[get_media_service] = lambda: MediaService(MediaRepository(b.tasks.sessions), storage)
    app.dependency_overrides[optional_user] = lambda: b.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        url = completed.result_payload["download_url"]
        for _ in range(2):
            response = await client.get(url, headers={"Range": "bytes=0-3"})
            assert response.status_code == 206 and len(response.content) == 4
        assert (await client.get(url, headers={"Range": "bytes=0-1,4-5"})).status_code == 416
    daily = b.quotas.balance(b.user.id)["daily"]
    assert daily["downloads_used"] == 1 and daily["egress_used_bytes"] == 8


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
async def test_actual_audio_duration_and_subtitle_free(billing, storage):
    from app.modules.transcript.worker import execute_transcription
    from app.modules.transcript.repository import TranscriptRepository
    from app.modules.transcript.data import make_transcript
    from app.modules.media.repository import MediaRepository
    b = billing
    task = b.create("transcription", 3)

    async def acquire(payload, directory, **controls):
        path = directory / "audio.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 16001)
        return path

    provider = SimpleNamespace(model="test-asr", available=True,
        transcribe=AsyncMock(return_value=make_transcript("zh", [{"start": 0, "end": 1.0001, "text": "有效转写"}])))
    result = await execute_transcription(task.id, TaskService(b.tasks, MemoryBus()), TranscriptRepository(b.tasks.sessions),
        MediaRepository(b.tasks.sessions), storage, SimpleNamespace(fetch=AsyncMock(return_value=None)),
        SimpleNamespace(acquire=acquire), provider)
    assert result["status"] == "completed", b.tasks.get(task.id)
    assert b.quotas.balance(b.user.id)["balances"]["asr_seconds"]["available"] == 1798
    repeat = b.create("transcription", 3)
    await execute_transcription(repeat.id, TaskService(b.tasks, MemoryBus()), TranscriptRepository(b.tasks.sessions),
        MediaRepository(b.tasks.sessions), storage, Mock(), Mock(), provider)
    assert b.quotas.balance(b.user.id)["balances"]["asr_seconds"]["available"] == 1798
    assert provider.transcribe.await_count == 1


async def test_bundle_failure_no_charge_then_success_grants_both_results(billing, monkeypatch):
    from app.modules.ai.worker import execute_ai
    from app.modules.ai.repository import AIRepository
    from app.modules.ai.providers.heuristic import HeuristicLLMProvider
    from app.modules.transcript.repository import TranscriptRepository
    from app.modules.transcript.data import make_transcript
    b = billing
    monkeypatch.setattr(settings, "LLM_PROVIDER", "heuristic")
    task = b.create("ai_mindmap")
    with b.tasks.sessions.begin() as session:
        transcript = TranscriptRepository(b.tasks.sessions).store(session, b.video_id, task.id, "subtitle",
            make_transcript("zh", [{"start": 0, "end": 60, "text": "任务成功后才核销额度，失败释放。"}]), "v1", "none")

    class FailMindmap(HeuristicLLMProvider):
        async def generate(self, request):
            if request.purpose == "mindmap":
                raise AppException("LLM_API_ERROR", "fixture failure", 502)
            return await super().generate(request)

    ai = AIRepository(b.tasks.sessions)
    service = TaskService(b.tasks, MemoryBus())
    pipeline = SimpleNamespace(acquire=AsyncMock(return_value=transcript))
    first = await execute_ai(task.id, service, ai, pipeline, FailMindmap())
    assert first["status"] == "failed"
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 5
    intermediate = ai.find_cache(transcript.content_hash, "summary", "heuristic-v1", settings.SUMMARY_PROMPT_VERSION, "zh")
    with pytest.raises(AppException):
        await service.authorize_artifact("result_id", intermediate.id, task.id, b.user)
    retry = b.create("ai_mindmap")
    assert (await execute_ai(retry.id, service, ai, pipeline, HeuristicLLMProvider()))["status"] == "completed"
    result = b.tasks.get(retry.id).result_payload
    assert result["summary_result_id"] == str(intermediate.id)
    for identifier in (result["result_id"], result["summary_result_id"]):
        await service.authorize_artifact("result_id", UUID(identifier), task.id, b.user)
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 4
    # All remaining AI allowance spent: viewing/reusing an owned bundle still needs zero.
    with b.tasks.sessions.begin() as session:
        for lot in session.scalars(select(Allowance).where(Allowance.kind == "ai_generations")):
            lot.used = lot.granted
    resolver = SimpleNamespace(resolve=AsyncMock(return_value={"video_id": str(b.video_id), "url": "https://example.com/watch"}))
    billing_service = BillingService(b.quotas, resolver, b.videos, service)
    quote = await billing_service.quote(b.user.id, "https://example.com/watch", "ai_summary", "video_720_mp4", "zh")
    assert quote["maximum"]["ai_generations"] == 0 and quote["maximum"]["asr_seconds"] == 0


async def test_dispatch_ack_loss_preserves_durable_operation(billing):
    b = billing
    task = b.create()
    _, payload, _ = b.quotas.quote(b.user.id, UUID(task.input_payload["quote_id"]))
    from app.modules.auth.dependencies import task_user_id
    marker = task_user_id.set(b.user.id)
    try:
        service = TaskService(b.tasks, MemoryBus(), Mock(enqueue=Mock(side_effect=TimeoutError)))
        response = await service.create(task.task_type, task.video_id, payload)
    finally:
        task_user_id.reset(marker)
    assert response.id == task.id and response.status == "queued"
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["held"] == 0


def test_unsubscribe_preserves_paid_allowances_and_release_after_disable(billing):
    b = billing
    b.quotas.grant(b.user.id, "asr_120", "paid-asr", "test")
    b.quotas.grant(b.user.id, "ai_100", "paid-ai", "test")
    b.users.subscription("allowance-test-identity", False)
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 100
    task = b.create()
    assert b.tasks.start(task.id)
    with b.tasks.sessions.begin() as session:
        session.get(User, b.user.id).status = "disabled"
    assert b.tasks.finish(task.id, "failed", 0).status == "failed"
    with b.tasks.sessions() as session:
        assert all(lot.held == 0 for lot in session.scalars(select(Allowance)))


def test_recovery_releases_only_after_worker_and_process_group_exit(billing, storage, monkeypatch):
    from app.modules.billing.manage import recover
    import app.modules.billing.manage as management
    b = billing
    task = b.create()
    assert b.tasks.start(task.id)
    monkeypatch.setattr(management.os, "kill", lambda *args: None)
    assert recover(b.tasks, storage.root) == 0

    def dead(*args):
        raise ProcessLookupError

    monkeypatch.setattr(management.os, "kill", dead)
    directory = storage.create_task_directory(task.id)
    (directory / ".process.json").write_text('{"pgid": 999999}')
    monkeypatch.setattr(management.ProcessRunner, "group_exists", lambda pgid: True)
    assert recover(b.tasks, storage.root) == 0
    monkeypatch.setattr(management.ProcessRunner, "group_exists", lambda pgid: False)
    assert recover(b.tasks, storage.root) == 1
    assert recover(b.tasks, storage.root) == 0
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 5


async def test_llm_operation_budget_limits_all_steps():
    from app.modules.ai.providers.base import LLMResult
    from app.modules.ai.providers.bounded import BoundedLLMProvider
    raw = SimpleNamespace(name="fake", model="fake", max_tokens=4096,
        generate=AsyncMock(return_value=LLMResult({}, 10, 0, 1, "fake")))
    provider = BoundedLLMProvider(raw, {"max_calls": 2, "total_output_tokens": 4096, "output_tokens_per_call": 4096})
    await provider.generate(None)
    await provider.generate(None)
    with pytest.raises(AppException, match="规模上限"):
        await provider.generate(None)
    assert raw.generate.await_count == 2


async def test_production_cannot_mint_allowances_through_mock_login(billing, monkeypatch):
    b = billing
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "WECHAT_MP_MODE", "mock")
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post(f"/api/v1/auth/wechat/sessions/{uuid4()}/mock-confirm")
        assert response.status_code == 403
    with pytest.raises(AppException, match="模拟公众号身份"):
        b.quotas.balance(b.user.id)
