"""yt-dlp implementation of Downloader. No business logic, only adapter code."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from backend.app.providers.downloader import (
    DownloadCancelled,
    DownloadProgress,
    FormatOption,
    VideoInfo,
    run_blocking,
)

BLOCKED_MARKERS = (
    "private video",
    "video unavailable",
    "not available",
    "unsupported url",
    "no video formats found",
    "sign in to confirm",
    "login required",
)


def classify_error(message: str) -> tuple[str, bool]:
    """Return (error_code, retryable). Business failures never retry."""
    lowered = message.lower()
    if "requested format not available" in lowered:
        return ("format_unavailable", False)
    if any(m in lowered for m in BLOCKED_MARKERS):
        return ("platform_blocked", False)
    if "timed out" in lowered or "timeout" in lowered or "connection" in lowered:
        return ("timeout", True)
    return ("download_failed", True)


def _pick_formats(info: dict[str, Any]) -> list[FormatOption]:
    options: list[FormatOption] = []
    for f in info.get("formats") or []:
        fid = f.get("format_id")
        if not fid:
            continue
        height = f.get("height")
        options.append(
            FormatOption(
                format_id=str(fid),
                ext=str(f.get("ext") or ""),
                resolution=f"{height}p" if height else f.get("resolution"),
                filesize=f.get("filesize") or f.get("filesize_approx"),
                vcodec=f.get("vcodec"),
                acodec=f.get("acodec"),
            )
        )
    return options


class YTDLPDownloader:
    def __init__(self, socket_timeout: int = 20) -> None:
        self._socket_timeout = socket_timeout

    def _base_opts(
        self, proxy: str | None, cookie_file: Path | None
    ) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self._socket_timeout,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookie_file:
            opts["cookiefile"] = str(cookie_file)
        return opts

    async def extract_info(self, url: str) -> VideoInfo:
        def _run() -> VideoInfo:
            try:
                with yt_dlp.YoutubeDL(self._base_opts(None, None)) as ydl:
                    info = ydl.extract_info(url, download=False)
            except ExtractorError as e:
                raise _as_public_error(str(e), "unsupported_platform")
            except DownloadError as e:
                raise _as_public_error(str(e), "parse_failed")
            if not info:
                raise _as_public_error("empty info", "parse_failed")
            return VideoInfo(
                platform=str(info.get("extractor_key") or info.get("extractor") or "unknown"),
                platform_video_id=str(info.get("id") or ""),
                title=str(info.get("title") or ""),
                uploader=info.get("uploader"),
                thumbnail=info.get("thumbnail"),
                duration_seconds=info.get("duration"),
                webpage_url=str(info.get("webpage_url") or url),
                formats=_pick_formats(info),
                raw_info=info,
            )

        return await run_blocking(_run)

    async def download(
        self,
        url: str,
        format_id: str,
        dest_dir: Path,
        on_progress: Callable[[DownloadProgress], None],
        should_cancel: Callable[[], bool],
        proxy: str | None = None,
        cookie_file: Path | None = None,
    ) -> Path:
        def hook(d: dict[str, Any]) -> None:
            if should_cancel():
                raise DownloadCancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100.0) if total else 0.0
                on_progress(
                    DownloadProgress(
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        percent=percent,
                        speed_bps=d.get("speed"),
                    )
                )

        def _run() -> Path:
            opts = self._base_opts(proxy, cookie_file)
            opts.update(
                {
                    "format": format_id,
                    "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "progress_hooks": [hook],
                }
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except DownloadCancelled:
                raise
            except (DownloadError, ExtractorError) as e:
                code, retryable = classify_error(str(e))
                raise PublicDownloadError(code, str(e), retryable)
            files = sorted(dest_dir.iterdir(), key=lambda p: p.stat().st_size, reverse=True)
            if not files:
                raise PublicDownloadError("download_failed", "no output file", True)
            return files[0]

        return await run_blocking(_run)


class PublicDownloadError(Exception):
    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _as_public_error(message: str, code: str) -> PublicDownloadError:
    mapped, _ = classify_error(message)
    if mapped in ("platform_blocked", "format_unavailable"):
        return PublicDownloadError(mapped, message, False)
    return PublicDownloadError(code, message, False)
