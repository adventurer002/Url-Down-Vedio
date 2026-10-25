"""yt-dlp boundary: metadata only, isolated process, bounded output and lifetime."""

import asyncio
import json
import math
import os
from pathlib import Path
import signal
import sys

from app.core.exceptions import AppException
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.parsers.runtime import javascript_runtime_options

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
                        and any(track.get("ext") in {"vtt", "srt", "json3", "ttml", "ass", "srv1", "srv2", "srv3"}
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


def extraction_options(proxy: str | None) -> dict:
    return {
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
        "js_runtimes": javascript_runtime_options(), "remote_components": [],
    }


def map_extraction_error(exc: Exception) -> AppException:
    if isinstance(exc, AppException):
        return exc
    # SDK text is used only for classification; never return it to the caller.
    detail = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in detail or "timeout" in detail:
        return AppException("PARSE_TIMEOUT", "视频元数据解析超时，请稍后重试", 504)
    if any(text in detail for text in ("confirm you're not a bot", "confirm you’re not a bot", "http error 403", "captcha")):
        return AppException("SOURCE_ACCESS_RESTRICTED", "视频平台要求额外验证或限制了服务器访问，请稍后重试", 502)
    if any(text in detail for text in ("http error 429", "too many requests", "rate limit")):
        return AppException("SOURCE_RATE_LIMITED", "视频平台请求频率受限，请稍后重试", 503)
    if any(text in detail for text in ("no supported javascript runtime", "challenge solving failed", "signature extraction failed")):
        return AppException("YTDLP_RUNTIME_UNAVAILABLE", "服务器视频解析运行时不可用，请联系管理员", 503)
    if any(text in detail for text in ("connection refused", "network is unreachable", "proxy error", "getaddrinfo failed")):
        return AppException("SOURCE_NETWORK_UNAVAILABLE", "服务器暂时无法连接视频平台，请稍后重试", 502)
    if any(text in detail for text in ("unsupported url", "no suitable extractor", "no video", "playlist", "sign in", "login", "private video", "not available", "video unavailable", "not a valid url")):
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
        process = None
        try:
            async with asyncio.timeout(self.timeout):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "app.modules.video.parsers.ytdlp_worker", url,
                    cwd=Path(__file__).resolve().parents[4],
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
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
                        "UNSUPPORTED_URL": ("链接不受支持或不是公开免登录视频", 422),
                        "PARSE_TIMEOUT": ("视频元数据解析超时，请稍后重试", 504),
                        "PARSE_FAILED": ("无法解析视频元数据，请稍后重试", 502),
                        "YTDLP_PROXY_INVALID": ("服务器视频网络出口配置无效，请联系管理员", 503),
                        "YTDLP_RUNTIME_UNAVAILABLE": ("服务器视频解析运行时不可用，请联系管理员", 503),
                        "SOURCE_ACCESS_RESTRICTED": ("视频平台要求额外验证或限制了服务器访问，请稍后重试", 502),
                        "SOURCE_RATE_LIMITED": ("视频平台请求频率受限，请稍后重试", 503),
                        "SOURCE_NETWORK_UNAVAILABLE": ("服务器暂时无法连接视频平台，请稍后重试", 502),
                    }
                    code = result["error"]
                    if code not in errors:
                        code = "PARSE_FAILED"
                    message, status = errors[code]
                    raise AppException(code, message, status)
                return ParsedVideo(**result["data"])
        except TimeoutError:
            raise map_extraction_error(TimeoutError()) from None
        except (ValueError, KeyError, TypeError, OSError):
            raise AppException("PARSE_FAILED", "视频解析进程返回无效结果", 502) from None
        finally:
            if process is not None:
                await _terminate_process_group(process)
