"""SSRF protection. Called in api layer AND re-checked in worker before download."""

import ipaddress
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlparse

from backend.app.core.errors import AppError

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}


@dataclass
class SafeURL:
    url: str
    host: str


def _reject(code: str, message: str) -> AppError:
    return AppError(code, message)


def _ip_forbidden(ip: IPv4Address | IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> SafeURL:
    """Raise AppError(invalid_url/url_not_allowed) when the URL is unsafe."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise _reject("invalid_url", "URL 为空或超过长度上限")
    try:
        parts = urlparse(url)
    except ValueError:
        raise _reject("invalid_url", "URL 格式非法")
    if parts.scheme not in ALLOWED_SCHEMES:
        raise _reject("url_not_allowed", "仅允许 http/https 协议")
    host = parts.hostname
    if not host:
        raise _reject("invalid_url", "URL 缺少主机名")
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise _reject("invalid_url", "域名无法解析")
    if not addrinfo:
        raise _reject("invalid_url", "域名无法解析")
    for family, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise _reject("url_not_allowed", "目标地址不被允许")
        if _ip_forbidden(ip):
            raise _reject("url_not_allowed", "目标地址为内网或保留地址")
    return SafeURL(url=url, host=host)
