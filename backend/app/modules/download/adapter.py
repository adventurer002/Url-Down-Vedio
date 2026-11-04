from pathlib import Path
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
        extension = "mp3" if payload["preset_id"] == "audio_mp3" else "mp4"
        output = directory / f"output.{extension}"
        if output.is_symlink() or not output.is_file() or output.stat().st_size == 0:
            from app.core.exceptions import AppException
            raise AppException("DOWNLOAD_FAILED", "下载进程未生成有效文件", 502)
        return output
