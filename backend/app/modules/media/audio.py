from pathlib import Path
import shutil
from uuid import UUID

from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.storage.base import StorageAdapter
from app.modules.download.adapter import YtDlpDownloadAdapter
from app.modules.download.presets import SAFE_PROTOCOLS
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import database_call
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.service import _has_codec, positive_number


def select_audio_stream(video: ParsedVideo) -> dict:
    streams = [item for item in video.formats if not item.get("has_drm") and item.get("url")
               and item.get("protocol", "https") in SAFE_PROTOCOLS
               and _has_codec(item, "acodec") and not _has_codec(item, "vcodec")]
    if not streams:
        raise AppException("AUDIO_STREAM_UNAVAILABLE", "视频没有可独立拉取的音轨，已阻止下载完整视频", 422)
    return max(streams, key=lambda item: positive_number(item.get("abr")) or positive_number(item.get("tbr")) or 0)


class AudioAcquirer:
    def __init__(self, media: MediaRepository, storage: StorageAdapter, runner: ProcessRunner):
        self.media, self.storage, self.runner = media, storage, runner

    async def candidates(self, video_id: UUID):
        items = await database_call(self.media.reusable, video_id, utcnow())
        valid = []
        for item in items:
            try:
                path = self.storage.resolve_file(item.storage_key)
                if path.is_file() and path.stat().st_size == item.size_bytes:
                    valid.append((item, path))
            except AppException:
                pass
        return sorted(valid, key=lambda pair: 0 if pair[0].mime_type == "audio/mpeg" else 1 if pair[0].mime_type.startswith("audio/") else 2)

    async def reuse_result(self, task):
        for item, path in await self.candidates(task.video_id):
            if item.mime_type == "audio/mpeg":
                return {"file_id": str(item.id), "download_url": f"/api/v1/files/{item.id}/download",
                        "filename": item.filename, "size_bytes": item.size_bytes, "expires_at": item.expires_at.isoformat(),
                        "reused": True}
        return None

    async def acquire(self, payload: dict, directory: Path, **controls) -> Path:
        for item, path in await self.candidates(UUID(payload["video_id"])):
            if item.mime_type == "audio/mpeg":
                return path
        return await self.download(payload, directory, **controls)

    async def download(self, payload: dict, directory: Path, **controls) -> Path:
        candidates = await self.candidates(UUID(payload["video_id"]))
        if candidates:
            source = candidates[0][1]
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise AppException("FFMPEG_UNAVAILABLE", "服务端缺少 FFmpeg", 503)
            output = directory / "output.mp3"
            await controls["on_progress"]({"stage": "processing", "progress": 40})
            await self.runner.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                   "-protocol_whitelist", "file,pipe,crypto", "-i", str(source),
                                   "-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-threads", "1", str(output)],
                                  directory, cwd=Path(__file__).resolve().parents[3], **controls)
            if not output.is_file() or not output.stat().st_size:
                raise AppException("AUDIO_EXTRACTION_FAILED", "音频提取未生成有效文件", 502)
            return output
        return await YtDlpDownloadAdapter(self.runner).download({**payload, "preset_id": "audio_mp3", "_audio_only": True}, directory, **controls)
