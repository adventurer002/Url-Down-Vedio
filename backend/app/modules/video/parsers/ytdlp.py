"""yt-dlp boundary: metadata only, isolated process, bounded output and lifetime."""

import asyncio
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile

from app.core.exceptions import AppException
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.parsers.runtime import javascript_runtime_options
from app.core.config import settings

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
PARSE_TIMEOUT = 30.0


def metadata_from_info(info: dict) -> ParsedVideo:
    if not isinstance(info, dict) or info.get("_type", "video") != "video" or not info.get("id"):
        raise AppException("UNSUPPORTED_URL", "仅支持单个公开免登录视频", 422)
    if info.get("is_live") or info.get("availability") in {"private", "premium_only", "subscriber_only", "needs_auth"}:
        raise AppException("UNSUPPORTED_URL", "仅支持非直播的公开免登录视频", 422)
    platform = str(info.get("extractor_key") or info.get("extractor") or "").split(":")[0].lower()
    if not platform or platform == "generic":
        raise AppException("UNSUPPORTED_URL", "暂不支持该链接的平台", 422)
    languages = sorted({language for key in ("subtitles", "automatic_captions")
                        for language, tracks in (info.get(key) or {}).items()
                        if language not in {"danmaku", "live_chat"}
                        and any(track.get("ext") in {"vtt", "srt", "json3", "ttml", "ass", "srv1", "srv2", "srv3", "bilibili_json"}
                                for track in tracks)})
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration < 0:
        duration = None
    return ParsedVideo(
        platform=platform, platform_video_id=str(info["id"]),
        title=info.get("title") or str(info["id"]), duration=duration,
        thumbnail=info.get("thumbnail"), uploader=info.get("uploader") or info.get("channel"),
        subtitle_languages=languages, formats=info.get("formats") or [],
    )


def extraction_options(proxy: str | None, *, url: str | None = None) -> dict:
    options = {
        "skip_download": True, "simulate": True, "noplaylist": True,
        "socket_timeout": 15, "retries": 1, "extractor_retries": 1,
        "fragment_retries": 0, "file_access_retries": 0,
        "cachedir": False, "quiet": True, "no_warnings": True,
        "proxy": proxy or "",  # Disable implicit HTTP_PROXY / system proxies.
        "cookiefile": None, "cookiesfrombrowser": None,
        "usenetrc": False, "enable_file_urls": False,
        "allowed_extractors": ["default", "-generic"],
        "check_formats": False, "writesubtitles": False, "writeautomaticsub": False,
        "writethumbnail": False, "writeinfojson": False,
        "js_runtimes": (javascript_runtime_options(url) if settings.VIDEO_EGRESS_MODE == "platform"
                        else javascript_runtime_options()), "remote_components": [],
    }
    if settings.VIDEO_EGRESS_MODE == "platform" and url is not None:
        from app.modules.video.parsers.links import platform_for
        if platform_for(url) in {"youtube", "douyin"}:
            options.update(retries=0, extractor_retries=0)
    return options


def source_failure(exc: Exception) -> str | None:
    """Only explicit source evidence affects shared circuit state; bare 403 is ambiguous."""
    known = getattr(exc, "source_failure", None)
    if known in {"restricted", "rate_limited"}:
        return known
    detail = str(exc).lower()
    if any(value in detail for value in ("http error 429", "too many requests", "rate limit")):
        return "rate_limited"
    if any(value in detail for value in ("http error 412", "confirm you're not a bot", "confirm you’re not a bot",
                                        "captcha", "触发哔哩哔哩安全风控", "request is blocked (-352)")):
        return "restricted"
    return None


def map_extraction_error(exc: Exception) -> AppException:
    if isinstance(exc, AppException):
        return exc
    # SDK text is used only for classification; never return it to the caller.
    detail = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in detail or "timeout" in detail:
        return AppException("PARSE_TIMEOUT", "视频元数据解析超时，请稍后重试", 504)
    if any(text in detail for text in ("confirm you're not a bot", "confirm you’re not a bot", "http error 403",
                                       "http error 412", "captcha", "触发哔哩哔哩安全风控", "request is blocked (-352)")):
        return AppException("SOURCE_ACCESS_RESTRICTED", "视频平台要求额外验证或限制了服务器访问，请稍后重试", 502)
    if any(text in detail for text in ("http error 429", "too many requests", "rate limit")):
        return AppException("SOURCE_RATE_LIMITED", "视频平台请求频率受限，请稍后重试", 503)
    if any(text in detail for text in ("no supported javascript runtime", "challenge solving failed", "signature extraction failed")):
        return AppException("YTDLP_RUNTIME_UNAVAILABLE", "服务器视频解析运行时不可用，请联系管理员", 503)
    if any(text in detail for text in ("connection refused", "network is unreachable", "proxy error", "getaddrinfo failed")):
        return AppException("SOURCE_NETWORK_UNAVAILABLE", "服务器暂时无法连接视频平台，请稍后重试", 502)
    if any(text in detail for text in ("unsupported url", "no suitable extractor", "no video", "playlist", "sign in", "login", "private video", "not available", "video unavailable", "video is unavailable", "removed by the uploader", "not a valid url")):
        return AppException("UNSUPPORTED_URL", "链接不受支持或不是公开免登录视频", 422)
    return AppException("PARSE_FAILED", "无法解析视频元数据，请稍后重试", 502)


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    async def discard_output():
        while await process.stdout.read(65536):
            pass

    # Drain pipes while stopping: Process.wait() alone can deadlock on a full pipe.
    drain = asyncio.create_task(discard_output())
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            pass
        # Also terminate descendants if the leader exited before its children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        async with asyncio.timeout(2):
            await process.wait()
            await drain
    finally:
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)


class YtDlpParser:
    def __init__(self, timeout: float = PARSE_TIMEOUT):
        self.timeout = timeout

    async def parse(self, url: str) -> ParsedVideo:
        from app.modules.video.parsers.links import canonical_link, resolve_short_link
        from app.modules.video.parsers.operations import source_operation
        url = canonical_link(url)
        try:
            async with source_operation(url, timeout=self.timeout, metadata=True) as lease:
                if settings.VIDEO_EGRESS_MODE == "platform":
                    async with asyncio.timeout(lease.metadata_remaining):
                        url = await resolve_short_link(url)
                return await self._parse(url, min(self.timeout, lease.metadata_remaining))
        except TimeoutError:
            raise map_extraction_error(TimeoutError()) from None

    async def _parse(self, url: str, timeout: float) -> ParsedVideo:
        process = None
        browser_directory = None
        process_options = {}
        from app.modules.video.parsers.links import platform_for
        if settings.VIDEO_EGRESS_MODE == "platform" and settings.DOUYIN_SOURCE_BACKEND == "browser" and platform_for(url) == "douyin":
            browser_directory = tempfile.TemporaryDirectory(prefix="vhdy-meta-")
            process_options["env"] = {**os.environ, "VIDEOHUB_BROWSER_WORK_DIRECTORY": browser_directory.name}
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "app.modules.video.parsers.ytdlp_worker", url,
                    cwd=Path(__file__).resolve().parents[4],
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    **process_options,
                )
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT_BYTES:
                        raise AppException("PARSE_FAILED", "解析结果超过大小限制", 502)
                await process.wait()
                if process.returncode != 0:
                    raise AppException("PARSE_FAILED", "视频解析进程异常退出", 502)
                result = json.loads(output)
                if "error" in result:
                    errors = {
                        "SSRF_BLOCKED": ("禁止访问非公开网络地址", 400),
                        "INVALID_URL": ("目标地址无效或无法解析", 400),
                        "DNS_TIMEOUT": ("YouTube DNS 查询超时，请稍后重试", 504),
                        "UNSUPPORTED_URL": ("链接不受支持或不是公开免登录视频", 422),
                        "PARSE_TIMEOUT": ("视频元数据解析超时，请稍后重试", 504),
                        "PARSE_FAILED": ("无法解析视频元数据，请稍后重试", 502),
                        "YTDLP_PROXY_INVALID": ("服务器视频网络出口配置无效，请联系管理员", 503),
                        "YTDLP_RUNTIME_UNAVAILABLE": ("服务器视频解析运行时不可用，请联系管理员", 503),
                        "SOURCE_ACCESS_RESTRICTED": ("视频平台要求额外验证或限制了服务器访问，请稍后重试", 502),
                        "SOURCE_RATE_LIMITED": ("视频平台请求频率受限，请稍后重试", 503),
                        "SOURCE_NETWORK_UNAVAILABLE": ("服务器暂时无法连接视频平台，请稍后重试", 502),
                        "SOURCE_FORMATS_UNAVAILABLE": ("视频平台公开接口结构暂不兼容，请稍后重试或上传文件", 502),
                        "SOURCE_RESPONSE_TOO_LARGE": ("视频平台响应超过大小限制", 502),
                        "SOURCE_BROWSER_UNAVAILABLE": ("服务器抖音浏览器运行时尚未就绪", 503),
                        "SOURCE_EGRESS_UNAVAILABLE": ("服务器尚未配置独立的 YouTube 出口，可改用上传文件", 503),
                        "VIDEO_ID_CHANGED": ("解析结果与请求平台不一致", 422),
                    }
                    code = result["error"]
                    if code not in errors:
                        code = "PARSE_FAILED"
                    message, status = errors[code]
                    error = AppException(code, message, status)
                    if result.get("source_failure") in {"restricted", "rate_limited"}:
                        error.source_failure = result["source_failure"]
                    if result.get("source_stage") in {"session", "view", "wbi", "play", "subtitle"}:
                        error.source_stage = result["source_stage"]
                    raise error
                return ParsedVideo(**result["data"])
        except TimeoutError:
            raise map_extraction_error(TimeoutError()) from None
        except (ValueError, KeyError, TypeError, OSError):
            raise AppException("PARSE_FAILED", "视频解析进程返回无效结果", 502) from None
        finally:
            if process is not None:
                await _terminate_process_group(process)
            if browser_directory is not None:
                browser_directory.cleanup()
