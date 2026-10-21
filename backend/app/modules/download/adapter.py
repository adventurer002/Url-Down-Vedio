from pathlib import Path
import sys
import json
from typing import Protocol

from app.infrastructure.process_runner import ProcessRunner


class DownloadAdapter(Protocol):
    async def download(self, payload: dict, directory: Path, **controls) -> Path: ...


class YtDlpDownloadAdapter:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    async def download(self, payload: dict, directory: Path, **controls) -> Path:
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
