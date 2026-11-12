"""Local-only media operations through bounded, process-group supervised adapters."""
from pathlib import Path
import shutil
import sys
from typing import Protocol

from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner


class ClipMediaProbe(Protocol):
    async def inspect(self, source: Path, directory: Path, **controls) -> dict: ...


class ClipRenderer(Protocol):
    async def render(self, source: Path, output: Path, info: dict, start: float, end: float, **controls) -> dict: ...


class FFmpegClipAdapter:
    def __init__(self, timeout, max_bytes):
        self.runner = ProcessRunner(timeout, max_bytes)
        self.features = None

    async def run(self, command, directory, failure, **controls):
        try:
            return await self.runner.run(command, directory, cwd=Path(__file__).resolve().parents[3], **controls)
        except AppException as error:
            if error.code == "DOWNLOAD_FAILED":
                raise AppException(failure, "切片媒体处理失败，请检查源文件与 FFmpeg 配置", 502) from None
            if error.code == "DOWNLOAD_TIMEOUT":
                raise AppException("CLIP_MEDIA_TIMEOUT", "切片媒体处理超过执行时限", 504) from None
            raise

    async def inspect(self, source, directory, **controls):
        result = await self.run([sys.executable, "-m", "app.modules.clips.media_child", "probe", str(source)],
            directory, "CLIP_MEDIA_INVALID", **controls)
        if "media" not in result:
            raise AppException("CLIP_MEDIA_INVALID", "未能读取视频精确时间轴", 422)
        return result["media"]

    @staticmethod
    def executable():
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AppException("FFMPEG_UNAVAILABLE", "服务端缺少 FFmpeg", 503)
        return ffmpeg

    @staticmethod
    def input_args(source, info, start):
        # copyts preserves source timestamps after fast seek. Filters cut at absolute PTS.
        return ["-copyts", "-ss", str(max(0, start - 2)), "-protocol_whitelist", "file,pipe",
                "-format_whitelist", "mov,matroska,webm,mp3", "-i", str(source)]

    async def audio_window(self, source, output, info, start, end, directory, **controls):
        absolute_start, absolute_end = info["origin"] + start, info["origin"] + end
        command = [self.executable(), "-nostdin", "-v", "error", "-y", *self.input_args(source, info, start),
            "-map", f"0:{info['audio_index']}", "-vn", "-af",
            f"atrim=start={absolute_start:.6f}:end={absolute_end:.6f},asetpts=PTS-STARTPTS",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "1", str(output)]
        await self.run(command, directory, "CLIP_TIMING_UNRELIABLE", **controls)
        if not output.is_file() or output.stat().st_size < 44:
            raise AppException("CLIP_TIMING_UNRELIABLE", "候选区间没有可定位的音频", 422)

    @staticmethod
    def render_input_args(source, info, start):
        # Keep the preceding keyframe and its held frames even after fast input seeking.
        return ["-noaccurate_seek", *FFmpegClipAdapter.input_args(source, info, start)]

    async def render(self, source, output, info, start, end, **controls):
        if self.features is None:
            capabilities = await self.run(
                [sys.executable, "-m", "app.modules.clips.media_child", "features"],
                output.parent, "FFMPEG_UNAVAILABLE", **controls)
            self.features = capabilities.get("features", {})
        if self.features.get("fps_mode"):
            frame_sync = ["-fps_mode", "vfr"]
        elif self.features.get("vsync"):
            frame_sync = ["-vsync", "vfr"]
        else:
            raise AppException("FFMPEG_UNAVAILABLE", "FFmpeg 缺少可用的帧率同步参数", 503)
        absolute_start, absolute_end = info["origin"] + start, info["origin"] + end
        # Anchor the output grid at the cut. Global-grid rounding could choose the next
        # 30 fps source frame instead of the frame actually held at a fractional cut.
        filters = (f"[0:{info['video_index']}]setpts=PTS-{absolute_start:.6f}/TB,"
            f"fps=30:round=up,trim=start=0:end={end - start:.6f},"
            "scale=w='max(2,trunc(min(1280,min(720,ih)*dar)/2)*2)':"
            "h='max(2,trunc(min(720,min(1280,iw*sar)/dar)/2)*2)',setsar=1[v];"
            f"[0:{info['audio_index']}]atrim=start={absolute_start:.6f}:end={absolute_end:.6f},"
            f"asetpts=PTS-{absolute_start:.6f}/TB[a]")
        command = [self.executable(), "-nostdin", "-v", "error", "-y", *self.render_input_args(source, info, start),
            "-filter_complex_threads", "1", "-filter_complex", filters,
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-threads", "1",
            *frame_sync, "-movflags", "+faststart", str(output)]
        await self.run(command, output.parent, "CLIP_RENDER_FAILED", **controls)
        result = await self.inspect(output, output.parent, **controls)
        if abs(result["duration"] - (end - start)) > .5:
            raise AppException("CLIP_VALIDATION_FAILED", "成片时长与选定区间不一致", 502)
        expected_offset = max(0, info["audio_start"] - info["origin"] - start) - max(
            0, info["video_start"] - info["origin"] - start)
        if abs((result["audio_start"] - result["video_start"]) - expected_offset) > .15:
            raise AppException("CLIP_VALIDATION_FAILED", "成片音视频时间轴偏移异常", 502)
        # Decode the complete bounded clip; container metadata alone cannot prove validity.
        await self.run([self.executable(), "-nostdin", "-v", "error", "-xerror",
            "-protocol_whitelist", "file,pipe", "-i", str(output), "-map", "0:v:0", "-map", "0:a:0",
            "-threads", "1", "-f", "null", "-"], output.parent, "CLIP_VALIDATION_FAILED", **controls)
        return {"duration": result["duration"], "sha256": result["sha256"], "decoded": True,
                "audio_video_offset": result["audio_start"] - result["video_start"]}
