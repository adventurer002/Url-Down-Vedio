import asyncio
import json
import socket
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.core import security
from app.modules.video.parsers import youtube_dns, ytdlp_worker


def reply(record_type, address=None):
    return {"Status": 0, "Answer": [] if address is None else [{"type": record_type, "data": address}]}


@pytest.mark.parametrize("value,record_type", [("127.0.0.1", 1), ("10.2.3.4", 1), ("169.254.169.254", 1),
    ("::1", 28), ("2001::1", 28), ("::ffff:127.0.0.1", 28)])
def test_doh_answers_never_bypass_public_ip_validation(value, record_type):
    with pytest.raises(AppException) as error:
        youtube_dns.addresses_from_response(json.dumps(reply(record_type, value)), record_type)
    assert error.value.code == "SSRF_BLOCKED"


def test_sync_dns_cache_and_budget_are_bounded():
    calls = []
    def respond(request):
        calls.append(request)
        kind = int(request.url.params["type"])
        return httpx.Response(200, json=reply(kind, "8.8.8.8" if kind == 1 else None))
    client = httpx.Client(transport=httpx.MockTransport(respond))
    resolver = youtube_dns.ProxiedDnsResolver("http://private-proxy:17892", Mock(), "private-proxy", client=client)
    try:
        assert resolver.getaddrinfo("www.youtube.com", 443)[0][4] == ("8.8.8.8", 443)
        resolver.getaddrinfo("www.youtube.com", 443)
        assert len(calls) == 2
        resolver.request_limit = resolver.requests
        with pytest.raises(AppException) as error:
            resolver.getaddrinfo("youtubei.googleapis.com", 443)
        assert error.value.code == "PARSE_FAILED" and len(calls) == 2
    finally:
        resolver.close()


def test_sync_dns_source_deadline_stops_new_network_requests():
    client = Mock()
    resolver = youtube_dns.ProxiedDnsResolver("http://proxy:1", Mock(), "proxy", client=client, deadline=lambda: 0)
    with pytest.raises(AppException) as error:
        resolver.getaddrinfo("www.youtube.com", 443)
    assert error.value.code == "DNS_TIMEOUT"
    client.stream.assert_not_called()


async def test_dns_connect_can_finish_after_three_seconds_within_lookup_budget():
    def respond(request):
        timeout = request.extensions["timeout"]
        if timeout["connect"] < 4.2:
            raise httpx.ConnectTimeout("Simulated proxy TLS connection", request=request)
        assert timeout["connect"] <= youtube_dns.LOOKUP_TIMEOUT
        assert timeout["read"] <= 2
        kind = int(request.url.params["type"])
        return httpx.Response(200, json=reply(kind, "8.8.8.8" if kind == 1 else None))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        resolver = youtube_dns.ProxiedDnsResolver("http://proxy:1", Mock(), "proxy", client=client)
        assert resolver.getaddrinfo("www.youtube.com", 443)[0][4] == ("8.8.8.8", 443)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        records = await youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1", client=client)
        assert records[0][4] == ("8.8.8.8", 443)


async def test_total_dns_budget_still_cancels_stalled_body(monkeypatch):
    closed = asyncio.Event()

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    monkeypatch.setattr(youtube_dns, "LOOKUP_TIMEOUT", .03)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=StalledBody()))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppException) as error:
            await youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1", client=client)
    assert error.value.code == "DNS_TIMEOUT"
    assert closed.is_set()


async def test_api_reuses_connections_but_revalidates_fresh_answers(monkeypatch):
    connections, calls = [], []

    def create(proxy):
        def respond(request):
            calls.append(request)
            kind = int(request.url.params["type"])
            address = "8.8.8.8" if len(calls) <= 2 else "127.0.0.1"
            return httpx.Response(200, json=reply(kind, address if kind == 1 else None))

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        connections.append((proxy, client))
        return client

    monkeypatch.setattr(youtube_dns, "dns_client", create)
    async with youtube_dns.api_dns_connections():
        await youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1")
        with pytest.raises(AppException) as error:
            await youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1")
        assert error.value.code == "SSRF_BLOCKED"
        assert len(connections) == 1 and not connections[0][1].is_closed
    assert connections[0][1].is_closed
    assert getattr(asyncio.get_running_loop(), "_videohub_dns_connections", None) is None


async def test_api_keeps_proxy_connections_isolated_and_closes_each(monkeypatch):
    connections = []

    def create(proxy):
        def respond(request):
            kind = int(request.url.params["type"])
            return httpx.Response(200, json=reply(kind, "8.8.8.8" if kind == 1 else None))

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        connections.append((proxy, client))
        return client

    monkeypatch.setattr(youtube_dns, "dns_client", create)
    async with youtube_dns.api_dns_connections():
        for proxy in ("http://proxy:1", "http://proxy:2", "http://proxy:1"):
            await youtube_dns.resolve_public("www.youtube.com", 443, proxy)
        assert [proxy for proxy, _ in connections] == ["http://proxy:1", "http://proxy:2"]
    assert all(client.is_closed for _, client in connections)


@pytest.mark.parametrize("proxy", ["socks4://proxy.example:1080", "socks5://proxy.example:1080"])
async def test_encrypted_dns_requires_the_supported_http_proxy_transport(proxy):
    with pytest.raises(AppException) as error:
        await youtube_dns.resolve_public("www.youtube.com", 443, proxy)
    assert error.value.code == "YTDLP_PROXY_INVALID"


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
    httpx.Response(200, content=b"x" * (youtube_dns.MAX_RESPONSE + 1)), httpx.Response(407, text="private credential detail")])
async def test_parent_dns_rejects_redirects_large_responses_and_proxy_errors(response):
    calls = []
    def respond(request):
        calls.append(request)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as client:
        with pytest.raises(AppException) as error:
            await youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1", client=client)
    assert error.value.code == "SOURCE_NETWORK_UNAVAILABLE" and len(calls) == 1
    assert "credential" not in error.value.message


async def test_parent_and_child_resolvers_validate_both_address_families():
    def respond(request):
        kind = int(request.url.params["type"])
        address = "8.8.8.8" if kind == 1 else "2606:4700:4700::1111"
        return httpx.Response(200, json=reply(kind, address))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        records = await youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1", client=client)
    assert {record[0] for record in records} == {socket.AF_INET, socket.AF_INET6}


async def test_source_dns_is_scoped_to_platform_youtube_and_never_sends_private_urls(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "YOUTUBE_DNS_MODE", "proxy_doh")
    original = AsyncMock(side_effect=lambda url: url)
    encrypted = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(security, "validate_url", original)
    monkeypatch.setattr(youtube_dns, "resolve_public", encrypted)
    import app.modules.video.parsers.runtime as runtime
    monkeypatch.setattr(runtime, "server_proxy", lambda url: "http://operator-proxy:17892")
    assert await security.validate_source_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    encrypted.assert_awaited_once()
    await security.validate_source_url("https://www.bilibili.com/video/BV1g2JX6nESP")
    original.assert_awaited_once()
    with pytest.raises(AppException): await security.validate_source_url("http://169.254.169.254/")
    assert encrypted.await_count == 1


async def test_cancelled_dns_closes_the_response_stream():
    reading, closed = asyncio.Event(), asyncio.Event()
    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b""
        async def aclose(self): closed.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=WaitingBody()))) as client:
        task = asyncio.create_task(youtube_dns.resolve_public("www.youtube.com", 443, "http://proxy:1", client=client))
        await asyncio.wait_for(reading.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert closed.is_set()


def test_guard_uses_checked_dns_and_restores_socket_and_media_budget(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "YOUTUBE_DNS_MODE", "proxy_doh")
    def original(host, port, **kwargs):
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001::1", port, 0, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", original)
    guard = ytdlp_worker.NetworkGuard("http://127.0.0.1:17892")
    guard.platform = "youtube"
    guard.public_dns.client.close()
    def respond(request):
        kind = int(request.url.params["type"])
        return httpx.Response(200, json=reply(kind, "8.8.8.8" if kind == 1 else None))
    guard.public_dns.client = httpx.Client(transport=httpx.MockTransport(respond))
    with guard.installed():
        guard.check_url("https://www.youtube.com/")
        assert guard.getaddrinfo("127.0.0.1", 17892)[0][4] == ("127.0.0.1", 17892)
        with guard.request_budget(32):
            assert guard.public_dns.request_limit == guard.public_dns.requests + 32
        assert guard.public_dns.request_limit is None
        with pytest.raises(AppException) as error: guard.check_url("http://169.254.169.254/")
        assert error.value.code == "SSRF_BLOCKED"
    assert socket.getaddrinfo is original and guard.public_dns.client.is_closed
