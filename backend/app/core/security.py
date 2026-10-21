"""Public HTTP URL validation. DNS probes have no retries and a bounded budget."""

import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.exceptions import AppException

DNS_TIMEOUT = 2.0
_TRACKING_KEYS = {"fbclid", "gclid", "si", "feature"}


def require_public_ip(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise AppException("INVALID_URL", "无法识别目标 IP 地址") from None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            require_public_ip(str(address.ipv4_mapped))
        # Transition mechanisms can route globally written addresses to private IPv4.
        if address.sixtofour or address.teredo or address in ipaddress.ip_network("64:ff9b::/96"):
            raise AppException("SSRF_BLOCKED", "禁止访问非公开网络地址")
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_link_local or address.is_loopback):
        raise AppException("SSRF_BLOCKED", "禁止访问非公开网络地址")


def normalize_url(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise AppException("INVALID_URL", "请输入有效的 HTTP 或 HTTPS URL")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise AppException("INVALID_URL", "URL 包含非法字符")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if parts.scheme.lower() not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
            raise ValueError
        if "%" in host:
            raise ValueError
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or port == 0:
            raise ValueError
    except (ValueError, UnicodeError):
        raise AppException("INVALID_URL", "请输入不含账号密码的 HTTP 或 HTTPS URL") from None
    if host == "localhost" or host.endswith(".localhost"):
        raise AppException("SSRF_BLOCKED", "禁止访问本地地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        require_public_ip(host)
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    # Preserve signed query strings byte for byte unless a known tracking key exists.
    query = parts.query
    pairs = parse_qsl(query, keep_blank_values=True)
    if any(key.lower().startswith("utm_") or key.lower() in _TRACKING_KEYS for key, _ in pairs):
        query = urlencode([(key, value) for key, value in pairs
                           if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS])
    normalized = urlunsplit((parts.scheme.lower(), authority, parts.path or "/", query, ""))
    return normalized, host, port or (443 if parts.scheme.lower() == "https" else 80)


def check_dns_results(results: list) -> None:
    if not results:
        raise AppException("INVALID_URL", "目标域名没有可用地址")
    for family, _, _, _, address in results:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise AppException("SSRF_BLOCKED", "禁止访问非公开网络地址")
        require_public_ip(address[0])


async def validate_url(url: str) -> str:
    normalized, host, port = normalize_url(url)
    try:
        async with asyncio.timeout(DNS_TIMEOUT):
            results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        check_dns_results(results)
    except TimeoutError:
        raise AppException("DNS_TIMEOUT", "域名解析超时，请稍后重试", 504) from None
    except OSError:
        raise AppException("INVALID_URL", "目标域名无法解析") from None
    return normalized
