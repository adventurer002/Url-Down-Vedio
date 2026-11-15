"""Delivery boundary, byte accounting and retry regressions with isolated public-HTTP fixtures."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from test_allowances import billing
from test_allowances_postgres import postgres
from test_download import storage, video, FixtureAdapter
from test_tasks import repository, MemoryBus
from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import validate_url
from app.main import create_app
from app.modules.auth.dependencies import get_auth_service
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import issue_token, issue_resource_access
from app.modules.billing.policy import now
from app.modules.billing.service import BillingService
from app.modules.download.delivery import DeliveryPreparer, DeliveryService
from app.modules.download.models import DownloadDelivery
from app.modules.download.relay import PublicRelayAdapter, RelayConnection, byte_range, mp4_codecs
from app.modules.download.repository import DeliveryRepository
from app.modules.download.router import get_delivery_service
from app.modules.download.worker import execute_download
from app.modules.media.repository import MediaRepository
from app.modules.tasks.service import TaskService


def box(kind, content=b""):
    return (len(content) + 8).to_bytes(4, "big") + kind + content


def mp4(*codecs):
    tracks = b"".join(box(b"trak", box(b"mdia", box(b"minf", box(b"stbl",
        box(b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + box(codec)))))) for codec in codecs)
    return box(b"ftyp", b"isom\0\0\0\0") + box(b"moov", tracks) + box(b"mdat", b"media" * 100)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data, self.closed = data, False

    async def __aiter__(self):
        for offset in range(0, len(self.data), 11):
            yield self.data[offset:offset + 11]

    async def aclose(self):
        self.closed = True


@pytest.fixture
def source():
    return {"url": "https://media.example/file.mp4?signature=private", "headers": {"Referer": "https://example.com/"},
        "duration_seconds": 61, "filename": "测试视频.mp4", "size_bytes": 100,
        "etag": '"fixture"', "range_supported": True, "delivery_mode": "relay"}


def prepared(b, source):
    task = b.create("download")
    assert b.tasks.start(task.id)
    result = {"delivery_id": str(task.id), "delivery_mode": source["delivery_mode"],
        "expires_at": (now() + timedelta(minutes=15)).isoformat()}
    b.tasks.complete_with(task.id, result, lambda session: DeliveryRepository.register(
        session, task, source, now() + timedelta(minutes=15)))
    return task


@pytest.mark.parametrize("header,expected", [(None, (0, 99)), ("bytes=10-19", (10, 19)),
    ("bytes=-10", (90, 99)), ("bytes=90-", (90, 99))])
def test_delivery_byte_ranges(header, expected):
    assert byte_range(header, 100) == expected


@pytest.mark.parametrize("header", ["bytes=-0", "bytes=100-", "bytes=20-10", "bytes=0-1,4-5", "bad"])
def test_invalid_delivery_ranges(header):
    with pytest.raises(AppException):
        byte_range(header, 100)


def test_metadata_box_validation_ignores_fake_codec_names_in_payload():
    assert mp4_codecs(mp4(b"avc1", b"mp4a")) == {b"avc1", b"mp4a"}
    assert mp4_codecs(box(b"ftyp", b"isom") + box(b"mdat", b"avc1mp4a")) == set()
    assert mp4_codecs(b"\0" * 64) == set()


async def test_probe_reads_bounded_media_metadata_and_closes_connection(monkeypatch):
    monkeypatch.setattr("app.modules.download.relay.validate_url", AsyncMock(side_effect=lambda url: url))
    content = mp4(b"avc1", b"mp4a")
    streams = []
    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["range"] == "bytes=0-1048575"
        stream = Chunks(content)
        streams.append(stream)
        return httpx.Response(206, headers={"Content-Length": str(len(content)),
            "Content-Range": f"bytes 0-{len(content)-1}/{len(content)}", "ETag": '"fixture"'}, stream=stream)
    adapter = PublicRelayAdapter(lambda: httpx.MockTransport(handle))
    inspected = await adapter.inspect({"url": "https://media.example/file.mp4", "headers": {}}, 10000)
    assert inspected["size_bytes"] == len(content) and inspected["delivery_mode"] == "relay"
    assert inspected["range_supported"] and streams[0].closed


@pytest.mark.parametrize("codecs", [(b"avc1",), (b"hvc1", b"mp4a"), (b"avc1", b"opus")])
async def test_probe_falls_back_for_missing_or_incompatible_tracks(monkeypatch, codecs):
    monkeypatch.setattr("app.modules.download.relay.validate_url", AsyncMock(side_effect=lambda url: url))
    content = mp4(*codecs)
    adapter = PublicRelayAdapter(lambda: httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"Content-Length": str(len(content))}, stream=Chunks(content))))
    assert await adapter.inspect({"url": "https://media.example/file.mp4"}, 10000) is None


async def test_redirect_to_private_network_never_connects(monkeypatch):
    async def check(url):
        return url if url.startswith("https://media.example/") else await validate_url(url)
    monkeypatch.setattr("app.modules.download.relay.validate_url", check)
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    adapter = PublicRelayAdapter(lambda: httpx.MockTransport(handle))
    with pytest.raises(AppException) as error:
        await adapter.request({"url": "https://media.example/file.mp4"}, None)
    assert error.value.code == "SSRF_BLOCKED" and len(requests) == 1


@pytest.mark.parametrize("status,headers", [(200, {}), (206, {"content-range": "bytes 0-9/100"}),
    (206, {"content-range": "bytes 10-19/100", "etag": '"changed"'})])
async def test_resume_rejects_ignored_ranges_or_changed_identity(monkeypatch, source, status, headers):
    monkeypatch.setattr("app.modules.download.relay.validate_url", AsyncMock(side_effect=lambda url: url))
    adapter = PublicRelayAdapter(lambda: httpx.MockTransport(lambda request: httpx.Response(status,
        headers={"content-length": "10", "etag": '"fixture"', **headers}, stream=Chunks(b"x" * 10))))
    with pytest.raises(AppException):
        await adapter.open(source, 1000, "bytes=10-19")


def test_preparation_claim_retry_and_partial_transfer_are_accounted_once(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 0
    lease = repository.reserve(task.id, b.user.id, None)
    with pytest.raises(AppException) as error:
        repository.reserve(task.id, b.user.id, None)
    assert error.value.code == "DELIVERY_BUSY"
    repository.finish(task.id, b.user.id, lease, 40, "DELIVERY_INTERRUPTED")
    repository.finish(task.id, b.user.id, lease, 99)
    retry = repository.reserve(task.id, b.user.id, "bytes=40-")
    repository.finish(task.id, b.user.id, retry, 60)
    daily = b.quotas.balance(b.user.id)["daily"]
    assert daily["downloads_used"] == 1
    assert daily["egress_used_bytes"] == 100 and daily["egress_held_bytes"] == 0


def test_failure_before_first_byte_refunds_download_and_traffic(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    lease = repository.reserve(task.id, b.user.id, None)
    repository.finish(task.id, b.user.id, lease, 0, "DELIVERY_UNAVAILABLE")
    daily = b.quotas.balance(b.user.id)["daily"]
    assert daily["downloads_used"] == daily["egress_held_bytes"] == daily["egress_used_bytes"] == 0
    with pytest.raises(AppException):
        repository.reserve(task.id, uuid4(), None)


async def test_fallback_replays_original_session_and_consumes_no_extra_download(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    lease = repository.reserve(task.id, b.user.id, None)
    repository.finish(task.id, b.user.id, lease, 20, "DELIVERY_INTERRUPTED")
    tasks = TaskService(b.tasks, MemoryBus(), SimpleNamespace(enqueue=lambda identifier: None))
    billing_service = BillingService(b.quotas, None, b.videos, tasks)
    service = DeliveryService(tasks, repository, billing_service)
    fallback = await service.fallback(task.id, b.user)
    assert fallback.input_payload["delivery_preference"] == "server"
    assert (await service.fallback(task.id, b.user)).id == fallback.id
    assert b.tasks.start(fallback.id)
    b.tasks.complete_with(fallback.id, {"file_id": str(uuid4())}, lambda session: None)
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 1
    with pytest.raises(AppException) as error:
        repository.reserve(task.id, b.user.id, None)
    assert error.value.code == "DELIVERY_FALLBACK_STARTED"


async def test_worker_prepares_delivery_without_downloading_full_file(billing, storage, source):
    b = billing
    task = b.create("download")
    tasks = TaskService(b.tasks, MemoryBus())
    downloader = SimpleNamespace(download=AsyncMock())
    result = await execute_download(task.id, tasks, MediaRepository(b.tasks.sessions), storage,
        downloader, preparer=SimpleNamespace(prepare=AsyncMock(return_value=source)))
    assert result["status"] == "completed"
    assert b.tasks.get(task.id).result_payload["delivery_id"] == str(task.id)
    downloader.download.assert_not_called()
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 0


async def test_preparer_rejects_split_tracks_and_server_preference(video, monkeypatch):
    monkeypatch.setattr("app.modules.download.delivery.server_proxy", lambda url: None)
    parser = SimpleNamespace(parse=AsyncMock(return_value=video))
    adapter = SimpleNamespace(inspect=AsyncMock(return_value={"delivery_mode": "relay"}))
    task = SimpleNamespace(task_type="download", user_id=uuid4(), input_payload={
        "url": "https://example.com/watch", "platform": "youtube", "platform_video_id": "fixture", "preset_id": "video_720_mp4"})
    assert await DeliveryPreparer(parser, adapter).prepare(task, 1000)
    task.input_payload["delivery_preference"] = "server"
    assert await DeliveryPreparer(parser, adapter).prepare(task, 1000) is None
    assert adapter.inspect.await_count == 1


async def test_download_endpoint_ticket_scope_range_bytes_and_authentication(billing, source, monkeypatch):
    b = billing
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "delivery-test-only-secret-at-least-32-characters")
    task = prepared(b, source)
    adapter = SimpleNamespace(open=AsyncMock())
    closed = []
    async def opened(descriptor, maximum, header):
        assert header == "bytes=10-19"
        response = httpx.Response(206, headers={"content-range": "bytes 10-19/100"}, stream=Chunks(b"x" * 10))
        client = httpx.AsyncClient()
        closed.append(client)
        return RelayConnection(client, response, 10, 100)
    adapter.open.side_effect = opened
    service = DeliveryService(TaskService(b.tasks, MemoryBus()), DeliveryRepository(b.tasks.sessions), adapter=adapter)
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: AuthService(b.users, None)
    app.dependency_overrides[get_delivery_service] = lambda: service
    endpoint = f"/api/v1/deliveries/{task.id}/download"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(endpoint)).status_code == 401
        auth = {"Authorization": "Bearer " + issue_token(b.user.id)}
        ticket = (await client.post(endpoint + "/access", headers=auth)).json()["data"]["access_token"]
        status = (await client.get(f"/api/v1/deliveries/{task.id}", headers=auth)).json()["data"]
        assert not status["active"] and not status["started"]
        assert "source" not in status and "url" not in status
        result = await client.get(endpoint, params={"access_token": ticket}, headers={"Range": "bytes=10-19"})
        assert result.status_code == 206 and result.content == b"x" * 10
        assert result.headers["x-accel-buffering"] == "no" and "attachment" in result.headers["content-disposition"]
        wrong = issue_resource_access(b.user.id, 0, "file_download", task.id)["access_token"]
        assert (await client.get(endpoint, params={"access_token": wrong})).status_code == 401
    assert closed[0].is_closed
    daily = b.quotas.balance(b.user.id)["daily"]
    assert daily["downloads_used"] == 1 and daily["egress_used_bytes"] == 10 and daily["egress_held_bytes"] == 0


async def test_verified_attachment_host_can_use_direct_delivery(monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_DIRECT_HOSTS", ["media.example"])
    monkeypatch.setattr("app.modules.download.relay.validate_url", AsyncMock(side_effect=lambda url: url))
    data = mp4(b"avc1", b"mp4a")
    adapter = PublicRelayAdapter(lambda: httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"Content-Length": str(len(data)), "Content-Disposition": "attachment"}, stream=Chunks(data))))
    source = {"url": "https://media.example/file.mp4", "headers": {}}
    assert (await adapter.inspect(source, 10000))["delivery_mode"] == "direct"
    assert (await adapter.inspect({**source, "headers": {"Referer": "https://example.com/"}}, 10000))["delivery_mode"] == "relay"


def test_member_zero_byte_failure_cannot_refund_another_free_download(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    first = repository.reserve(task.id, b.user.id, None)
    repository.finish(task.id, b.user.id, first, 100)
    b.quotas.grant(b.user.id, "pro_monthly", "membership", "test")
    other = prepared(b, source)
    lease = repository.reserve(other.id, b.user.id, None)
    assert not lease.free_download_charged
    repository.finish(other.id, b.user.id, lease, 0, "DELIVERY_UNAVAILABLE")
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 1


def test_relay_and_background_download_share_the_account_concurrency_limit(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    lease = repository.reserve(task.id, b.user.id, None)
    background = b.create("download")
    assert not b.tasks.start(background.id)
    assert b.tasks.get(background.id).wait_reason == "capacity"
    repository.finish(task.id, b.user.id, lease, 0, "DELIVERY_INTERRUPTED")
    assert b.tasks.start(background.id)
    with pytest.raises(AppException) as error:
        repository.reserve(task.id, b.user.id, None)
    assert error.value.code == "DELIVERY_BUSY"


async def test_fallback_after_an_unused_delivery_charges_one_download(billing, source):
    b = billing
    task = prepared(b, source)
    tasks = TaskService(b.tasks, MemoryBus(), SimpleNamespace(enqueue=lambda identifier: None))
    service = DeliveryService(tasks, DeliveryRepository(b.tasks.sessions), BillingService(b.quotas, None, b.videos, tasks))
    fallback = await service.fallback(task.id, b.user)
    assert b.tasks.start(fallback.id)
    b.tasks.complete_with(fallback.id, {"file_id": str(uuid4())}, lambda session: None)
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 1


async def test_source_failure_releases_transfer_and_reports_error_to_status(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    adapter = SimpleNamespace(open=AsyncMock(side_effect=httpx.ConnectError("private diagnostic")))
    service = DeliveryService(TaskService(b.tasks, MemoryBus()), repository, adapter=adapter)
    with pytest.raises(AppException) as error:
        await service.response(task.id, b.user, SimpleNamespace(headers={}))
    assert error.value.code == "DELIVERY_UNAVAILABLE"
    assert repository.get(task.id, b.user.id)["last_error"] == "DELIVERY_UNAVAILABLE"
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 0
    assert b.quotas.balance(b.user.id)["daily"]["egress_held_bytes"] == 0


@pytest.mark.parametrize("interrupted", [False, True])
async def test_first_chunk_is_sent_before_source_completes_and_cancel_releases_resources(billing, source, interrupted):
    from app.modules.download.delivery import MeteredRelayResponse
    from app.modules.download.relay import CHUNK_BYTES
    b = billing
    source = {**source, "size_bytes": CHUNK_BYTES * 2}
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    lease = repository.reserve(task.id, b.user.id, None)
    first_sent, release_source = asyncio.Event(), asyncio.Event()

    class SlowSource(Chunks):
        async def __aiter__(self):
            yield b"a" * CHUNK_BYTES
            await release_source.wait()
            yield b"b" * CHUNK_BYTES

    stream = SlowSource(b"")
    client = httpx.AsyncClient()
    connection = RelayConnection(client, httpx.Response(200, stream=stream), CHUNK_BYTES * 2, CHUNK_BYTES * 2)
    response = MeteredRelayResponse(connection, repository, task.id, b.user.id, lease)

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_sent.set()

    async def receive():
        await asyncio.Event().wait()

    sending = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    await asyncio.wait_for(first_sent.wait(), 2)
    assert not sending.done()  # The client has bytes while the source is still blocked.
    if interrupted:
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    else:
        release_source.set()
        await sending
    assert stream.closed and client.is_closed
    daily = b.quotas.balance(b.user.id)["daily"]
    assert daily["egress_held_bytes"] == 0
    assert daily["egress_used_bytes"] == CHUNK_BYTES * (1 if interrupted else 2)


def test_postgres_concurrent_session_claims_charge_one_download(postgres, source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    sessions, tasks, quotas, user, create = postgres
    b = SimpleNamespace(tasks=tasks, quotas=quotas, user=user, create=create)
    task = prepared(b, source)
    repository = DeliveryRepository(sessions)
    barrier = Barrier(2)
    def reserve(_):
        barrier.wait(timeout=3)
        try:
            return repository.reserve(task.id, user.id, None)
        except AppException as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, range(2)))
    assert results.count("DELIVERY_BUSY") == 1
    lease = next(result for result in results if not isinstance(result, str))
    assert quotas.balance(user.id)["daily"]["downloads_used"] == 1
    repository.finish(task.id, user.id, lease, 100)


async def test_postgres_concurrent_fallbacks_create_one_task(postgres, source):
    sessions, tasks, quotas, user, create = postgres
    b = SimpleNamespace(tasks=tasks, quotas=quotas, user=user, create=create)
    task = prepared(b, source)
    service_tasks = TaskService(tasks, MemoryBus(), SimpleNamespace(enqueue=lambda identifier: None))
    service = DeliveryService(service_tasks, DeliveryRepository(sessions),
        BillingService(quotas, None, SimpleNamespace(sessions=sessions), service_tasks))
    results = await asyncio.gather(service.fallback(task.id, user), service.fallback(task.id, user))
    assert len({result.id for result in results}) == 1


async def test_relay_logs_redact_unknown_signed_query_fields(monkeypatch, caplog):
    import logging
    from app.core.logging import install_log_redaction
    install_log_redaction()
    caplog.set_level(logging.INFO, logger="httpx")
    monkeypatch.setattr("app.modules.download.relay.validate_url", AsyncMock(side_effect=lambda url: url))
    adapter = PublicRelayAdapter(lambda: httpx.MockTransport(lambda request: httpx.Response(403, stream=Chunks(b""))))
    client, response = await adapter.request({"url": "https://media.example/file.mp4?cdn_unknown_key=private-value"}, None)
    await response.aclose()
    await client.aclose()
    assert "private-value" not in caplog.text


async def test_delivery_preparation_cancellation_waits_for_cleanup():
    from app.modules.download.delivery import prepare_with_cancellation
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def prepare(task, maximum):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    async def cancelled():
        return started.is_set()
    with pytest.raises(AppException) as error:
        await prepare_with_cancellation(SimpleNamespace(prepare=prepare), None, 100, cancelled)
    assert error.value.code == "TASK_CANCELLED" and cleaned.is_set()


async def test_worker_reuses_only_owned_matching_cached_download(billing, storage, source):
    b = billing
    service = TaskService(b.tasks, MemoryBus())
    media = MediaRepository(b.tasks.sessions)
    first = b.create("download")
    await execute_download(first.id, service, media, storage, FixtureAdapter())
    second = b.create("download")
    preparer = SimpleNamespace(prepare=AsyncMock(return_value=source))
    downloader = SimpleNamespace(download=AsyncMock())
    await execute_download(second.id, service, media, storage, downloader, preparer=preparer)
    result = b.tasks.get(second.id).result_payload
    assert result["cache_hit"] and result["file_id"] == b.tasks.get(first.id).result_payload["file_id"]
    preparer.prepare.assert_not_called()
    downloader.download.assert_not_called()
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 1
    other = b.users.subscription("other-cache-user", True, login=True)
    quote = b.quotas.save_quote(other.id, "download", {**{key: value for key, value in first.input_payload.items()
        if key not in {"quote_id", "allowance"}},
        "video_id": str(b.video_id)})
    _, payload, _ = b.quotas.quote(other.id, UUID(quote["quote_id"]))
    third = b.tasks.create("download", b.video_id, payload, now() + timedelta(hours=1), other.id)
    await execute_download(third.id, service, media, storage, downloader, preparer=preparer)
    assert b.tasks.get(third.id).result_payload["delivery_id"] == str(third.id)
    preparer.prepare.assert_awaited_once()


async def test_old_quote_clients_get_file_delivery_and_new_clients_opt_in(billing):
    from app.modules.auth.dependencies import required_user
    from app.modules.billing.router import get_billing_service
    b = billing
    service = BillingService(b.quotas, SimpleNamespace(resolve=AsyncMock(return_value={
        "video_id": str(b.video_id), "url": "https://example.com/watch", "platform": "youtube", "platform_video_id": "fixture"})),
        b.videos, TaskService(b.tasks, MemoryBus()))
    app = create_app()
    app.dependency_overrides[required_user] = lambda: b.user
    app.dependency_overrides[get_billing_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for preference in (None, "auto"):
            body = {"url": "https://example.com/watch", "action": "download", "preset_id": "video_720_mp4"}
            if preference:
                body["delivery_preference"] = preference
            response = await client.post("/api/v1/billing/quotes", json=body)
            assert response.status_code == 200
            identifier = UUID(response.json()["data"]["quote_id"])
            assert b.quotas.quote(b.user.id, identifier)[1]["delivery_preference"] == (preference or "server")


def test_native_download_preflight_reports_exhausted_quota_without_charging(billing, source):
    b = billing
    task = prepared(b, source)
    repository = DeliveryRepository(b.tasks.sessions)
    repository.validate_access(task.id, b.user.id)
    assert b.quotas.balance(b.user.id)["daily"]["downloads_used"] == 0
    for _ in range(2):
        other = b.create("download")
        assert b.tasks.start(other.id)
        b.tasks.complete_with(other.id, {"file_id": str(uuid4())}, lambda session: None)
    with pytest.raises(AppException) as error:
        repository.validate_access(task.id, b.user.id)
    assert error.value.code == "DAILY_DOWNLOADS_EXCEEDED"
