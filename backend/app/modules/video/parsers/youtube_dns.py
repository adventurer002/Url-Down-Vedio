"""Opt-in YouTube encrypted DNS through its operator-owned proxy.

The fixed public endpoint uses ordinary TLS verification. Every returned
address still passes the application's public-IP checks.
"""
import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import socket
import threading
import time
from typing import Callable, Protocol
from urllib.parse import urlsplit

import httpx

from app.core.exceptions import AppException
from app.core.security import require_public_ip

ENDPOINT = "https://223.5.5.5/resolve"
MAX_RESPONSE = 64 * 1024
LOOKUP_TIMEOUT = 8


@asynccontextmanager
async def api_dns_connections():
    """Own reusable DoH connections for one API lifespan, without caching answers."""
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_videohub_dns_connections", None)
    clients = {}
    loop._videohub_dns_connections = clients
    try:
        yield
    finally:
        loop._videohub_dns_connections = previous
        async with asyncio.timeout(2):
            await asyncio.gather(*(client.aclose() for client in clients.values()))


def dns_client(proxy):
    return httpx.AsyncClient(proxy=proxy, trust_env=False, follow_redirects=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30))


class PublicDnsResolver(Protocol):
    original: Callable
    requests: int
    request_limit: int | None
    def getaddrinfo(self, host, port, *args, **kwargs) -> list: ...
    def close(self) -> None: ...


def dns_unavailable():
    return AppException("SOURCE_NETWORK_UNAVAILABLE", "服务器 YouTube 加密 DNS 暂不可用，请稍后重试", 502)


def validate_dns_proxy(proxy):
    if not proxy or urlsplit(proxy).scheme != "http":
        raise AppException("YTDLP_PROXY_INVALID", "YouTube 加密 DNS 需要服务端 HTTP 代理端口", 503)


def addresses_from_response(body, record_type):
    try:
        data = json.loads(body)
        if data.get("Status") == 3:
            return []
        if data.get("Status") != 0:
            raise ValueError
        answers = data.get("Answer") or []
        if not isinstance(answers, list) or len(answers) > 64:
            raise ValueError
        addresses = []
        for answer in answers:
            if answer.get("type") != record_type:
                continue
            address = ipaddress.ip_address(answer["data"])
            if address.version != (4 if record_type == 1 else 6):
                raise ValueError
            require_public_ip(str(address))
            addresses.append(str(address))
        return addresses
    except (ValueError, TypeError, KeyError, AttributeError):
        raise dns_unavailable() from None


def address_records(addresses, port, family=0, type=0, proto=0):
    if not addresses:
        raise AppException("INVALID_URL", "目标域名没有可用公网地址", 400)
    records = []
    for value in dict.fromkeys(addresses):
        require_public_ip(value)
        version = ipaddress.ip_address(value).version
        address_family = socket.AF_INET if version == 4 else socket.AF_INET6
        if family not in {0, socket.AF_UNSPEC, address_family}:
            continue
        target = (value, int(port)) if version == 4 else (value, int(port), 0, 0)
        records.append((address_family, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", target))
    if not records:
        raise AppException("INVALID_URL", "目标域名没有所需类型的公网地址", 400)
    return records


class ProxiedDnsResolver:
    def __init__(self, proxy, original, proxy_host, *, client=None, deadline=lambda: None):
        validate_dns_proxy(proxy)
        self.original = original
        self.proxy_host = proxy_host.lower()
        self.client = client or httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False)
        self.deadline = deadline
        self.cache = {}
        self.lock = threading.RLock()
        self.requests = 0
        self.request_limit = None

    def close(self):
        self.client.close()

    def query(self, host, record_type, deadline):
        if self.request_limit is not None and self.requests >= self.request_limit:
            raise AppException("PARSE_FAILED", "YouTube DNS 查询达到取源预算上限", 502)
        self.requests += 1
        remaining = deadline - time.monotonic()
        source_deadline = self.deadline()
        if source_deadline is not None:
            remaining = min(remaining, source_deadline - time.monotonic())
        if remaining <= 0:
            raise AppException("DNS_TIMEOUT", "YouTube DNS 查询超过执行时限", 504)
        try:
            with self.client.stream("GET", ENDPOINT, params={"name": host, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                    timeout=httpx.Timeout(min(2, remaining), connect=min(5, remaining))) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise AppException("DNS_TIMEOUT", "YouTube DNS 查询超过执行时限", 504)
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise dns_unavailable()
            return addresses_from_response(body, record_type)
        except httpx.TimeoutException:
            raise AppException("DNS_TIMEOUT", "YouTube DNS 查询超时", 504) from None
        except httpx.HTTPError:
            raise dns_unavailable() from None

    def getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        text = host.decode("ascii") if isinstance(host, bytes) else host
        try:
            ipaddress.ip_address(text)
            literal = True
        except ValueError:
            literal = False
        # NetworkGuard separately validates literal targets and exempts only the
        # operator's proxy connection, preserving the existing trust boundary.
        if literal or text.lower() == self.proxy_host:
            return self.original(host, port, family=family, type=type, proto=proto, flags=flags)
        with self.lock:
            cached = self.cache.get(text)
            if cached and cached[0] > time.monotonic():
                addresses = cached[1]
            else:
                deadline = time.monotonic() + LOOKUP_TIMEOUT
                addresses = self.query(text, 1, deadline) + self.query(text, 28, deadline)
                self.cache[text] = (time.monotonic() + 30, addresses)
        return address_records(addresses, port, family, type, proto)


async def resolve_public(host, port, proxy, *, client=None):
    validate_dns_proxy(proxy)
    async def lookup(active_client):
        addresses = []
        async with asyncio.timeout(LOOKUP_TIMEOUT):
            for record_type in (1, 28):
                async with active_client.stream("GET", ENDPOINT, params={"name": host, "type": record_type},
                        headers={"Accept": "application/dns-json"}, timeout=httpx.Timeout(2, connect=5)) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE:
                            raise dns_unavailable()
                addresses.extend(addresses_from_response(body, record_type))
        return address_records(addresses, port)

    try:
        if client is not None:
            return await lookup(client)
        clients = getattr(asyncio.get_running_loop(), "_videohub_dns_connections", None)
        if clients is not None:
            if proxy not in clients:
                clients[proxy] = dns_client(proxy)
            return await lookup(clients[proxy])
        async with dns_client(proxy) as active_client:
            return await lookup(active_client)
    except (TimeoutError, httpx.TimeoutException):
        raise AppException("DNS_TIMEOUT", "YouTube DNS 查询超时", 504) from None
    except httpx.HTTPError:
        raise dns_unavailable() from None
