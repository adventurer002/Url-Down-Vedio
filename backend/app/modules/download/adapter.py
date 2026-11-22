from pathlib import Path
import re
import shutil
import sys
import json
from typing import Protocol

from app.infrastructure.process_runner import ProcessRunner
from app.core.config import settings
from app.modules.video.parsers.links import canonical_link, resolve_short_link
from app.modules.video.parsers.operations import source_operation
from app.core.exceptions import AppException
import asyncio


class DownloadAdapter(Protocol):
    async def download(self, payload: dict, directory: Path, **controls) -> Path: ...


class YtDlpDownloadAdapter:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    async def download(self, payload: dict, directory: Path, **controls) -> Path:
        try:
            async with asyncio.timeout(self.runner.timeout):
                return await self._with_optional_cache(payload, directory, **controls)
        except TimeoutError:
            raise AppException("DOWNLOAD_TIMEOUT", "下载超过总执行时限", 504) from None

    async def _with_optional_cache(self, payload, directory, **controls):
        from app.modules.video.parsers.source_cache import load
        cached = await load(payload)
        try:
            if cached:
                cache_file = directory / ".source-cache.json"
                cache_file.touch(mode=0o600, exist_ok=False)
                cache_file.write_bytes(cached.encode() if isinstance(cached, str) else cached)
                return await self._execute({**payload, "_cached_source": True}, directory, **controls)
        except AppException as error:
            if error.code != "DOWNLOAD_SOURCE_STALE":
                raise
            directory.mkdir(mode=0o700, exist_ok=True)
        finally:
            if cached:
                (directory / ".source-cache.json").unlink(missing_ok=True)
        return await self._execute(payload, directory, **controls)

    async def _execute(self, payload, directory, **controls):
        if settings.VIDEO_EGRESS_MODE == "platform":
            try:
                async with asyncio.timeout(self.runner.timeout):
                    async with source_operation(canonical_link(payload["url"]), timeout=self.runner.timeout,
                            cancelled=controls.get("is_cancel_requested")) as lease:
                        async with asyncio.timeout(lease.metadata_remaining):
                            url = await resolve_short_link(canonical_link(payload["url"]), cancelled=controls.get("is_cancel_requested"))
                        return await self._download({**payload, "url": url}, directory,
                            on_source_ready=lease.metadata_ready, source_metadata_timeout=lease.metadata_remaining, **controls)
            except TimeoutError:
                raise AppException("DOWNLOAD_TIMEOUT", "下载超过总执行时限", 504) from None
        return await self._download(payload, directory, **controls)

    async def _download(self, payload, directory, **controls):
        await self.runner.run(
            [sys.executable, "-m", "app.modules.download.child", json.dumps(payload), str(directory)],
            directory, cwd=Path(__file__).resolve().parents[3], **controls,
        )
        if payload.get("_defer_render"):
            manifest = directory / "download-sources.json"
            if manifest.is_symlink() or not manifest.is_file():
                raise AppException("DOWNLOAD_FAILED", "下载进程未生成媒体清单", 502)
            return manifest
        return checked_output(payload, directory)


def checked_output(payload, directory):
    extension = "mp3" if payload["preset_id"] == "audio_mp3" else "mp4"
    output = directory / f"output.{extension}"
    if output.is_symlink() or not output.is_file() or output.stat().st_size == 0:
        raise AppException("DOWNLOAD_FAILED", "下载进程未生成有效文件", 502)
    return output


class LocalRenderAdapter:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    async def download(self, payload, directory, **controls):
        from app.modules.download.child import ffmpeg_command

        manifest = directory / "download-sources.json"
        try:
            if manifest.is_symlink() or manifest.stat().st_size > 4096:
                raise ValueError
            data = json.loads(manifest.read_text())
            names, codecs = data["sources"], data["codecs"]
            if (not isinstance(names, list) or not 1 <= len(names) <= 2 or not isinstance(codecs, list)
                    or len(names) != len(codecs) or any(not isinstance(codec, dict) for codec in codecs)):
                raise ValueError
            sources = []
            for index, name in enumerate(names):
                if not re.fullmatch(rf"source_{index}\.[a-z0-9]+", name):
                    raise ValueError
                path = directory / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError
                sources.append(path)
        except (OSError, ValueError, KeyError, TypeError):
            raise AppException("DOWNLOAD_FAILED", "待处理媒体文件已失效", 502) from None
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AppException("FFMPEG_UNAVAILABLE", "服务端缺少 FFmpeg", 503)
        extension = "mp3" if payload["preset_id"] == "audio_mp3" else "mp4"
        output = directory / f"output.{extension}"
        if output.is_symlink():
            raise AppException("DOWNLOAD_FAILED", "处理输出路径无效", 502)
        if settings.DOWNLOAD_FAST_MP4_ENABLED and extension == "mp4" and len(sources) == 1 and sources[0].suffix == ".mp4":
            from app.modules.download.fast_mp4 import ready_mp4
            if await ready_mp4(sources[0], directory, **controls):
                sources[0].replace(output)
                return checked_output(payload, directory)
        await controls["on_progress"]({"stage": "rendering", "progress": 90})
        try:
            await self.runner.run(ffmpeg_command(sources, codecs, output, ffmpeg), directory, cwd=directory, **controls)
        except AppException as error:
            if error.code == "DOWNLOAD_TIMEOUT":
                raise AppException("DOWNLOAD_RENDER_TIMEOUT", "视频封装或转码超过执行时限", 504) from None
            raise
        return checked_output(payload, directory)
