"""Downloader abstraction. Business code must never import yt_dlp directly."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class FormatOption:
    format_id: str
    ext: str
    resolution: str | None = None
    filesize: int | None = None
    vcodec: str | None = None
    acodec: str | None = None


@dataclass
class VideoInfo:
    platform: str
    platform_video_id: str
    title: str
    uploader: str | None = None
    thumbnail: str | None = None
    duration_seconds: int | None = None
    webpage_url: str = ""
    formats: list[FormatOption] = field(default_factory=list)
    raw_info: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadProgress:
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    percent: float = 0.0
    speed_bps: float | None = None


class Downloader(Protocol):
    async def extract_info(self, url: str) -> VideoInfo: ...

    async def download(
        self,
        url: str,
        format_id: str,
        dest_dir: Path,
        on_progress: Callable[[DownloadProgress], None],
        should_cancel: Callable[[], bool],
        proxy: str | None = None,
        cookie_file: Path | None = None,
    ) -> Path: ...


class DownloadCancelled(Exception):
    pass


async def run_blocking[T](func: Callable[[], T]) -> T:
    return await asyncio.to_thread(func)
