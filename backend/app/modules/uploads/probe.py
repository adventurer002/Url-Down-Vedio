"""A bounded, local-only ffprobe adapter; runs exclusively in a Celery child group."""
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Protocol

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.modules.billing.policy import seconds


class MediaProbe(Protocol):
    async def inspect(self, path: Path, directory: Path, **controls) -> dict: ...


@dataclass
class FFprobeAdapter:
    runner: ProcessRunner

    async def inspect(self, path, directory, **controls):
        return await self.runner.run([sys.executable, "-m", "app.modules.uploads.probe", str(path)], directory,
            cwd=Path(__file__).resolve().parents[3], **controls)


def validate_probe(info, extension):
    names = set(info.get("format", {}).get("format_name", "").split(","))
    allowed = {"mp3": {"mp3"}, "m4a": {"mov", "mp4", "m4a"}, "mp4": {"mov", "mp4"}, "mov": {"mov"},
               "mkv": {"matroska"}, "webm": {"webm", "matroska"}}
    if not names.intersection(allowed[extension]):
        raise AppException("UPLOAD_FORMAT_MISMATCH", "文件实际格式与扩展名不匹配", 422)
    streams = info.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio" and s.get("codec_name") not in {None, "unknown"}]
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not audio:
        raise AppException("UPLOAD_NO_AUDIO", "文件没有可识别的音轨，无法进行语音转写", 422)
    if extension == "mp3":
        if video or any(s["codec_name"] != "mp3" for s in audio):
            raise AppException("UPLOAD_FORMAT_MISMATCH", "文件不是有效的 MP3 音频", 422)
    elif extension == "m4a":
        if video or any(s["codec_name"] not in {"aac", "alac"} for s in audio):
            raise AppException("UPLOAD_FORMAT_MISMATCH", "文件不是有效的 M4A 音频（支持 AAC、ALAC 编码）", 422)
    elif not video:
        raise AppException("UPLOAD_FORMAT_MISMATCH", "请上传含音轨的视频文件或 MP3、M4A 音频", 422)
    # Bill the selected audio track, not the last subtitle or the video track.
    durations = [s.get("duration") for s in audio[:1]] + [info.get("format", {}).get("duration")]
    for duration in durations:
        try:
            return {"duration_seconds": seconds(duration), "media_type": "audio" if extension in {"mp3", "m4a"} else "video"}
        except AppException:
            pass
    raise AppException("UPLOAD_INVALID_MEDIA", "无法确认音频时长", 422)


def main():
    try:
        executable = shutil.which("ffprobe")
        if not executable:
            raise AppException("FFMPEG_UNAVAILABLE", "服务端缺少 ffprobe", 503)
        result = subprocess.run([executable, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov,matroska,webm,mp3", "-probesize", "5000000", "-analyzeduration", "5000000",
            "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,duration:stream_disposition=attached_pic",
            "-of", "json", sys.argv[1]], capture_output=True, timeout=min(10, settings.UPLOAD_PROBE_TIMEOUT_SECONDS), check=True)
        print(json.dumps({"probe": json.loads(result.stdout)}), flush=True)
    except AppException as exc:
        print(json.dumps({"error_code": exc.code}), flush=True)
    except (subprocess.SubprocessError, ValueError, OSError):
        print(json.dumps({"error_code": "UPLOAD_INVALID_MEDIA"}), flush=True)


if __name__ == "__main__":
    main()
