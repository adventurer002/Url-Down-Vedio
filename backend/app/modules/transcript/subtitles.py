import json
from pathlib import Path
import sys
from typing import Protocol

from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.modules.transcript.data import TranscriptData
from app.modules.transcript.subtitle_parser import parse_subtitle


class SubtitleProvider(Protocol):
    async def fetch(self, payload: dict, directory: Path, **controls) -> TranscriptData | None: ...


class YtDlpSubtitleProvider:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    async def fetch(self, payload: dict, directory: Path, **controls) -> TranscriptData | None:
        result = await self.runner.run([sys.executable, "-m", "app.modules.transcript.subtitle_child",
                                        json.dumps(payload), str(directory)], directory,
                                       cwd=Path(__file__).resolve().parents[3], **controls)
        if result.get("subtitle_found") is False:
            return None
        filename = result.get("filename", "")
        path = directory / filename
        if (not filename or path.is_symlink() or path.resolve().parent != directory.resolve()
                or path.suffix not in {".vtt", ".srt", ".json3"} or not path.is_file()):
            raise AppException("SUBTITLE_FETCH_FAILED", "字幕进程未返回有效文件", 502)
        if path.stat().st_size > 5 * 1024 * 1024:
            raise AppException("SUBTITLE_TOO_LARGE", "字幕超过大小限制", 413)
        try:
            return parse_subtitle(path.read_text(encoding="utf-8-sig"), path.suffix[1:], payload.get("language") or result["language"])
        except AppException as exc:
            if exc.code == "TRANSCRIPT_EMPTY":
                return None
            raise
