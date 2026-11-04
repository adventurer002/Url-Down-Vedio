"""Pure link recognition plus bounded, direct-only domestic short-link resolution."""
import asyncio
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx

from app.core.exceptions import AppException
from app.core.security import normalize_url, validate_url
from app.infrastructure.http_client import public_transport

PLATFORMS = {"bilibili": ("bilibili.com", "b23.tv"),
             "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
             "douyin": ("douyin.com", "iesdouyin.com")}


def platform_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    for platform, domains in PLATFORMS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return "other"


def extract_link(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise AppException("INVALID_URL", "请输入一个有效的视频链接")
    text = value.strip()
    # Preserve strict handling of a standalone URL, including unsafe controls.
    if re.match(r"^https?://", text, re.I) and not re.search(r"\s", text):
        text = text.rstrip("，。；！、）】》")
        normalize_url(text)
        return text
    matches = re.findall(r'https?://[^\s<>"\u3000]+', text, re.I)
    if len(matches) != 1:
        raise AppException("INVALID_URL", "分享文本必须且只能包含一个视频链接")
    url = matches[0].rstrip("，。；！、）】》")
    normalize_url(url)
    return url


def canonical_link(value: str) -> str:
    url, host, _ = normalize_url(extract_link(value))
    parts = urlsplit(url)
    if host in {"douyin.com", "www.douyin.com", "m.douyin.com", "www.iesdouyin.com", "iesdouyin.com"}:
        query = parse_qs(parts.query, keep_blank_values=True)
        match = re.fullmatch(r"/(?:video|share/video)/([1-9][0-9]{0,23})/?", parts.path)
        modal = query.get("modal_id", [])
        if modal and (len(modal) != 1 or not re.fullmatch(r"[1-9][0-9]{0,23}", modal[0])):
            raise AppException("INVALID_URL", "抖音视频 ID 无效或重复")
        if match and modal and match[1] != modal[0]:
            raise AppException("INVALID_URL", "抖音链接包含不同的视频 ID")
        identifier = match[1] if match else (modal[0] if modal and parts.path in {"/", "/jingxuan", "/discover"} else None)
        if identifier:
            return "https://www.douyin.com/video/" + identifier
    if platform_for(url) == "youtube":
        query = parse_qs(parts.query, keep_blank_values=True)
        if host == "youtu.be" or host.endswith(".youtu.be"):
            identifier = parts.path.strip("/")
        elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
            identifier = parts.path.split("/")[2]
        elif parts.path == "/watch":
            identifier = query.get("v", [""])[0]
        else:
            return url
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
            raise AppException("INVALID_URL", "YouTube 视频 ID 无效")
        # Reject ambiguous multi-source query strings, never silently pick one.
        if len(query.get("v", [])) > 1:
            raise AppException("INVALID_URL", "视频链接包含重复的视频 ID")
        return "https://www.youtube.com/watch?" + urlencode({"v": identifier})
    return url


def is_domestic_short_link(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in {"b23.tv", "www.b23.tv", "v.douyin.com"}


async def resolve_short_link(url: str, *, transport=None, cancelled=None) -> str:
    if not is_domestic_short_link(url):
        return url
    operation = asyncio.create_task(_resolve_short_link(url, transport=transport))
    try:
        while not operation.done():
            if cancelled is not None:
                try:
                    async with asyncio.timeout(3):
                        if await cancelled():
                            raise AppException("TASK_CANCELLED", "任务已取消", 409)
                except TimeoutError:
                    raise AppException("DOWNLOAD_CONTROL_TIMEOUT", "取消检查超过执行时限", 503) from None
            await asyncio.wait({operation}, timeout=.5)
        return await operation
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def _resolve_short_link(url: str, *, transport=None) -> str:
    """Called only in platform mode, under a parent operation/metadata permit."""
    if not is_domestic_short_link(url):
        return url
    platform = platform_for(url)
    try:
        async with asyncio.timeout(8):
            async with httpx.AsyncClient(transport=transport or public_transport(), trust_env=False,
                    follow_redirects=False, timeout=httpx.Timeout(5, connect=3)) as client:
                for redirects in range(6):
                    url = await validate_url(url)
                    if platform_for(url) != platform:
                        raise AppException("UNSUPPORTED_URL", "短链跳转到了其他平台或不支持的域名", 422)
                    async with client.stream("GET", url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = response.headers.get("location")
                            if not target or redirects == 5:
                                raise AppException("UNSUPPORTED_URL", "短链跳转无效或次数过多", 422)
                            url = urljoin(url, target)
                            continue
                        if response.status_code in {412, 429}:
                            code = "SOURCE_RATE_LIMITED" if response.status_code == 429 else "SOURCE_ACCESS_RESTRICTED"
                            error = AppException(code, "视频平台暂时限制了服务器访问，请稍后重试", 503)
                            error.source_failure = "rate_limited" if response.status_code == 429 else "restricted"
                            raise error
                        if response.status_code >= 400 or is_domestic_short_link(url):
                            raise AppException("UNSUPPORTED_URL", "短链未能还原为公开视频链接", 422)
                        return canonical_link(url)
    except (TimeoutError, httpx.TimeoutException):
        raise AppException("PARSE_TIMEOUT", "短链还原超过执行时限", 504) from None
    except httpx.HTTPError:
        raise AppException("SOURCE_NETWORK_UNAVAILABLE", "服务器无法还原视频短链", 502) from None
