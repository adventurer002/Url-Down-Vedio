from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.logging import redact_urls
from app.infrastructure.storage.objects import S3ObjectStore, object_key, object_store, store_tag
from app.main import create_app
from app.modules.auth.dependencies import required_user, resource_user
from app.modules.billing.models import AllowanceTransfer
from app.modules.download.router import get_media_service
from app.modules.media import delivery
from app.modules.media.gateway import create_gateway
from app.modules.media.mirror import mirror_file
from app.modules.media.repository import MediaRecord, MediaRepository
from app.modules.media.service import MediaService
from app.modules.tasks.repository import utcnow
from test_allowances import billing
from test_download import storage, video
from test_tasks import repository


@pytest.fixture
def delivery_config(monkeypatch):
    for key, value in {
        "OBJECT_DELIVERY_ENABLED": True,
        "OBJECT_DELIVERY_SECRET": SecretStr("test-delivery-signing-secret-0123456789"),
        "OBJECT_GATEWAY_URL": "https://download.example.com",
        "OBJECT_BUCKET": "videohub-testing",
        "OBJECT_REGION": "example-region",
        "OBJECT_NAMESPACE": "isolated",
        "OBJECT_UPLOAD_ENDPOINT": "https://upload.example.com",
        "OBJECT_READ_ENDPOINT": "https://read.example.com",
        "OBJECT_ACCESS_KEY": SecretStr("testing-key"),
        "OBJECT_SECRET_KEY": SecretStr("testing-secret"),
    }.items():
        monkeypatch.setattr(settings, key, value)


@pytest.fixture
def downloadable(billing, storage, delivery_config, monkeypatch):
    task = billing.create("download")
    assert billing.tasks.start(task.id)
    file_id = uuid4()
    key = f"files/{file_id}.mp4"
    storage.resolve_file(key).write_bytes(b"0123456789")
    media = MediaRepository(billing.tasks.sessions)
    item = MediaRecord(file_id, task.id, task.video_id, "video_720_mp4", key, "测试.mp4", "video/mp4", 10,
                       utcnow() + timedelta(hours=1), "full_source")
    billing.tasks.complete_with(task.id, {"file_id": str(file_id)}, lambda session: media.register(session, item))
    api = create_app()
    api.dependency_overrides[get_media_service] = lambda: MediaService(media, storage)
    api.dependency_overrides[resource_user] = lambda: billing.user
    monkeypatch.setattr(delivery, "SessionLocal", billing.tasks.sessions)
    return SimpleNamespace(api=api, media=media, item=item, billing=billing,
                           path=f"/api/v1/files/{file_id}/download")


class Bytes(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"23"
        yield b"4"


class ObjectFixture:
    tag = property(lambda self: store_tag())

    def __init__(self):
        self.puts = []
        self.reads = []
        self.fail_put = False
        self.status = 206

    async def put(self, key, path, size, mime_type):
        self.puts.append((key, size, mime_type))
        if self.fail_put:
            raise AppException("OBJECT_UPLOAD_FAILED", "fixture upload failed", 502)

    @asynccontextmanager
    async def read(self, key, byte_range):
        self.reads.append((key, byte_range))
        yield httpx.Response(self.status, stream=Bytes(), headers={
            "Content-Length": "3", "Content-Range": "bytes 2-4/10"})


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.example")


class ASGICallbacks:
    def __init__(self, api):
        self.api = api

    async def report(self, identifier, action, amount):
        async with client(self.api) as api:
            response = await api.post(f"/api/v1/internal/object-delivery/{identifier}/{action}",
                json={"bytes": amount}, headers={"X-Object-Gateway-Key": delivery.delivery_secret()})
        if response.status_code != 200:
            error = response.json()["error"]
            raise AppException(error["code"], error["message"], response.status_code)


async def test_mirror_range_single_use_and_local_fallback(downloadable, storage):
    item, media, api = downloadable.item, downloadable.media, downloadable.api
    store = ObjectFixture()
    async with client(api) as session:
        local = await session.get(downloadable.path)
        assert local.status_code == 200 and local.content == b"0123456789"
        assert await mirror_file(item.id, media, storage, store)
        assert not await mirror_file(item.id, media, storage, store)
        other = downloadable.billing.users.subscription("other-download-owner", True, login=True)
        api.dependency_overrides[resource_user] = lambda: other
        assert (await session.get(downloadable.path)).status_code == 403
        api.dependency_overrides[resource_user] = lambda: downloadable.billing.user
        assert (await session.get(downloadable.path, headers={"Range": "bytes=10-99"})).status_code == 416
        redirect = await session.get(downloadable.path, headers={"Range": "bytes=2-4"})
        assert redirect.status_code == 307
        token = parse_qs(urlsplit(redirect.headers["location"]).query)["token"][0]
        identifier = delivery.decode_ticket(token).transfer_id
        forbidden = await session.post(f"/api/v1/internal/object-delivery/{identifier}/claim", json={"bytes": 3})
        assert forbidden.status_code == 403
        assert (await session.get(downloadable.path + "?delivery=local")).status_code == 200
        assert (await session.get(downloadable.path, headers={"Range": "bytes=2-4", "If-Range": '"invalid"'})).status_code == 200

    gateway = create_gateway(store_factory=lambda: store, callbacks=ASGICallbacks(api))
    async with client(gateway) as session:
        url = redirect.headers["location"]
        assert (await session.get(url, headers={"Range": "bytes=0-2"})).status_code == 416 and not store.reads
        sent = await session.get(url, headers={"Range": "bytes=2-4"})
        assert sent.status_code == 206 and sent.content == b"234"
        assert sent.headers["content-range"] == "bytes 2-4/10"
        assert (await session.get(url, headers={"Range": "bytes=2-4"})).status_code == 409
    assert len(store.puts) == len(store.reads) == 1
    with media.sessions() as session:
        transfer = session.get(AllowanceTransfer, identifier)
        assert transfer.sent == transfer.reserved == 3 and transfer.claimed_at is not None


async def test_failed_upload_keeps_local_and_bad_object_releases_claim(downloadable, storage):
    store = ObjectFixture()
    store.fail_put = True
    file_id = downloadable.item.id
    assert not await mirror_file(file_id, downloadable.media, storage, store)
    assert downloadable.media.get(file_id).object_store_tag is None
    async with client(downloadable.api) as session:
        assert (await session.get(downloadable.path)).status_code == 200
        assert downloadable.media.finish_mirror(file_id, store.tag, utcnow())
        storage.resolve_file(downloadable.item.storage_key).unlink()
        redirect = await session.get(downloadable.path, headers={"Range": "bytes=2-4"})
        assert redirect.status_code == 307
    store.status = 200
    gateway = create_gateway(store_factory=lambda: store, callbacks=ASGICallbacks(downloadable.api))
    async with client(gateway) as session:
        assert (await session.get(redirect.headers["location"], headers={"Range": "bytes=2-4"})).status_code == 502
    with downloadable.media.sessions() as session:
        remote = session.scalar(select(AllowanceTransfer).where(AllowanceTransfer.claim_expires_at.is_not(None)))
        assert remote.sent == 0 and remote.claimed_at is not None


async def test_direct_cos_link_charges_full_size_after_authorization(downloadable, storage, monkeypatch):
    item, media, api, billing = downloadable.item, downloadable.media, downloadable.api, downloadable.billing
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "isolated-resource-access-signing-secret")
    monkeypatch.setattr(settings, "OBJECT_DELIVERY_MODE", "direct")
    monkeypatch.setattr(settings, "OBJECT_GATEWAY_URL", "")
    monkeypatch.setattr(settings, "OBJECT_DELIVERY_SECRET", SecretStr(""))
    monkeypatch.setattr(settings, "OBJECT_BUCKET", "videohub-testing-1234567890")
    monkeypatch.setattr(settings, "OBJECT_REGION", "ap-chengdu")
    monkeypatch.setattr(settings, "OBJECT_READ_ENDPOINT", "https://cos.ap-chengdu.myqcloud.com")
    api.dependency_overrides[required_user] = lambda: billing.user
    quotas = billing.quotas
    owner = billing.user.id
    async with client(api) as session:
        local = await session.get(downloadable.path, headers={"Range": "bytes=2-4"})
        assert local.status_code == 206 and local.content == b"234"
        assert media.finish_mirror(item.id, store_tag(), utcnow())
        storage.resolve_file(item.storage_key).unlink()

        other = billing.users.subscription("direct-download-other", True, login=True)
        api.dependency_overrides[resource_user] = lambda: other
        assert (await session.get(downloadable.path)).status_code == 403
        api.dependency_overrides[resource_user] = lambda: billing.user
        assert (await session.post(downloadable.path + "/access")).status_code == 200
        assert (await session.get(downloadable.path, headers={"Range": "bytes=10-20"})).status_code == 416
        assert quotas.balance(owner)["daily"]["egress_used_bytes"] == 3

        redirect = await session.get(downloadable.path, headers={"Range": "bytes=2-4"})
        assert redirect.status_code == 307
        assert redirect.headers["cache-control"] == "private, no-store"
        assert redirect.headers["referrer-policy"] == "no-referrer"
        url = urlsplit(redirect.headers["location"])
        query = parse_qs(url.query)
        assert url.netloc == "videohub-testing-1234567890.cos.ap-chengdu.myqcloud.com"
        assert url.path == "/" + object_key(store_tag(), item.storage_key)
        assert query["X-Amz-Expires"] == ["120"]
        assert query["X-Amz-SignedHeaders"] == ["host"]
        assert query["response-content-disposition"][0].startswith("attachment; filename*=UTF-8''")
        daily = quotas.balance(owner)["daily"]
        assert (daily["egress_used_bytes"], daily["egress_held_bytes"]) == (13, 0)

        remaining = quotas.balance(owner)["limits"]["daily_egress_bytes"] - 13
        quotas.reserve_transfer(owner, remaining - 9)
        denied = await session.get(downloadable.path, headers={"Range": "bytes=2-4"})
        assert denied.status_code == 429
        assert "location" not in denied.headers
        charges = [event["detail"] for event in quotas.ledger(owner) if event["event"] == "object_link_issued"]
        assert charges == [{"file_id": str(item.id), "charged_bytes": 10}]


async def test_direct_link_expiry_and_missing_config_fallback(downloadable, monkeypatch):
    item, media = downloadable.item, downloadable.media
    monkeypatch.setattr(settings, "OBJECT_DELIVERY_MODE", "direct")
    assert media.finish_mirror(item.id, store_tag(), utcnow())
    monkeypatch.setattr(delivery, "utcnow", lambda: item.expires_at - timedelta(seconds=20))
    async with client(downloadable.api) as session:
        link = await session.get(downloadable.path)
        assert link.status_code == 307
        assert parse_qs(urlsplit(link.headers["location"]).query)["X-Amz-Expires"] == ["20"]
        monkeypatch.setattr(settings, "OBJECT_SECRET_KEY", SecretStr(""))
        fallback = await session.get(downloadable.path, headers={"Range": "bytes=2-4"})
        assert fallback.status_code == 206 and fallback.content == b"234"


def test_claim_expiry_dedup_and_reconciliation(billing):
    repo = billing.quotas
    unused = repo.reserve_transfer(billing.user.id, 10, claim_expires_at=utcnow() - timedelta(seconds=1))
    used = repo.reserve_transfer(billing.user.id, 20, claim_expires_at=utcnow() + timedelta(minutes=1))
    assert not repo.claim_delivery(unused, 10)
    assert not repo.claim_delivery(used, 19)
    assert repo.claim_delivery(used, 20)
    assert not repo.claim_delivery(used, 20)
    assert not repo.settle_delivery(used, 21)
    assert repo.settle_delivery(used, 7)
    assert repo.settle_delivery(used, 7)
    assert repo.reconcile_transfers() == 1
    with billing.tasks.sessions() as session:
        assert session.get(AllowanceTransfer, unused).sent == 0
        assert session.get(AllowanceTransfer, used).sent == 7


@pytest.mark.parametrize("cos", [False, True])
async def test_s3_endpoints_and_head_size_verification(tmp_path, delivery_config, monkeypatch, cos):
    if cos:
        endpoint = "https://cos.ap-chengdu.myqcloud.com"
        monkeypatch.setattr(settings, "OBJECT_BUCKET", "videohub-testing-1234567890")
        monkeypatch.setattr(settings, "OBJECT_REGION", "ap-chengdu")
        monkeypatch.setattr(settings, "OBJECT_UPLOAD_ENDPOINT", endpoint)
        monkeypatch.setattr(settings, "OBJECT_READ_ENDPOINT", endpoint)
    upload, read = object_store(upload=True), object_store()
    file = tmp_path / "sample"
    file.write_bytes(b"sample-bytes")
    requests = []

    async def respond(request):
        requests.append(request)
        assert request.url.params.get("X-Amz-Signature")
        if request.method == "PUT":
            assert request.headers["content-length"] == "12" and await request.aread() == file.read_bytes()
            return httpx.Response(200)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "12"})
        assert request.headers["Range"] == "bytes=2-4"
        return httpx.Response(206, headers={"Content-Length": "3"})

    transport = httpx.MockTransport(respond)
    upload = S3ObjectStore(upload.bucket, upload.tag, client=upload.client, transport=transport)
    read = S3ObjectStore(read.bucket, read.tag, client=read.client, transport=transport)
    key = object_key(read.tag, f"files/{uuid4()}.mp4")
    await upload.put(key, file, 12, "video/mp4")
    async with read.read(key, "bytes=2-4") as response:
        assert response.status_code == 206
    hosts = ["upload.example.com", "upload.example.com", "read.example.com"]
    path = f"/{read.bucket}/{key}"
    if cos:
        hosts = [f"{read.bucket}.cos.ap-chengdu.myqcloud.com"] * 3
        path = f"/{key}"
    assert [r.url.host for r in requests] == hosts
    assert all(request.url.path == path for request in requests)
    assert "testing-key" not in redact_urls(str(requests[0].url))
    assert requests[0].url.params["X-Amz-Signature"] not in redact_urls(str(requests[0].url))


async def test_gateway_disconnect_settles_only_sent_bytes(delivery_config):
    from starlette.requests import ClientDisconnect
    from app.modules.media.gateway import MeteredObjectResponse

    delivered = delivery.Delivery(
        transfer_id=uuid4(), storage_key=f"files/{uuid4()}.mp4", tag=store_tag(), size=10,
        bounds=(2, 4), filename="fixture.mp4", mime_type="video/mp4",
    )
    reports = []

    async def report(identifier, action, amount):
        reports.append((identifier, action, amount))

    async def send(message):
        if message.get("body") == b"4":
            raise OSError("client gone")

    async def receive():
        raise AssertionError("ASGI 2.4 handles disconnect via send")

    response = MeteredObjectResponse(delivered, ObjectFixture(), SimpleNamespace(report=report))
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert reports == [(delivered.transfer_id, "claim", 3), (delivered.transfer_id, "settle", 2)]
