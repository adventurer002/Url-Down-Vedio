"""Private download process. FFmpeg children inherit this process group."""

from contextlib import contextmanager, redirect_stdout
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.download.presets import select_streams
from app.modules.video.parsers.ytdlp import extraction_options, metadata_from_info
from app.modules.video.parsers.ytdlp_worker import NetworkGuard, guarded_downloader


@contextmanager
def native_network_only():
    # HlsFD can internally fall back to FFmpeg even when selected as native.
    # Deny that fallback: FFmpeg must never fetch URLs outside the guarded transport.
    from yt_dlp.downloader import hls
    original = hls.FFmpegFD

    class NoNetworkFFmpegFD(original):
        @classmethod
        def available(cls, path=None):
            return False

        def real_download(self, filename, info_dict):
            raise AppException("UNSUPPORTED_URL", "此流需要不受支持的外部网络下载器", 422)

    hls.FFmpegFD = NoNetworkFFmpegFD
    try:
        yield
    finally:
        hls.FFmpegFD = original


def ffmpeg_command(sources: list[Path], streams: list[dict], output: Path, ffmpeg: str) -> list[str]:
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        command += ["-protocol_whitelist", "file,pipe,crypto", "-i", str(source)]
    if output.suffix == ".mp3":
        return command + ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-threads", "1", str(output)]
    audio_index = 1 if len(sources) == 2 else 0
    video_copy = str(streams[0].get("vcodec", "")).startswith(("avc1", "h264"))
    audio_copy = str(streams[audio_index].get("acodec", "")).startswith(("mp4a", "aac"))
    command += ["-map", "0:v:0", "-map", f"{audio_index}:a:0", "-c:v", "copy" if video_copy else "libx264"]
    if not video_copy:
        command += ["-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    command += ["-c:a", "copy" if audio_copy else "aac"]
    if not audio_copy:
        command += ["-b:a", "192k"]
    return command + ["-threads", "1", "-movflags", "+faststart", str(output)]


def download(payload: dict, directory: Path, emit) -> None:
    started = time.monotonic()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AppException("FFMPEG_UNAVAILABLE", "服务端缺少 FFmpeg", 503)
    guard = NetworkGuard(os.environ.get("YTDLP_PROXY"))
    last_emit = 0.0
    with guard.installed(), native_network_only():
        guard.check_url(payload["url"])
        options = extraction_options(os.environ.get("YTDLP_PROXY"))
        with guarded_downloader(guard, options) as parser:
            info = parser.extract_info(payload["url"], download=False)
        if guard.violation:
            raise guard.violation
        video = metadata_from_info(info)
        if (video.platform, video.platform_video_id) != (payload["platform"], payload["platform_video_id"]):
            raise AppException("VIDEO_ID_CHANGED", "视频身份发生变化", 422)
        streams = select_streams(video, payload["preset_id"])
        sources = []
        for index, stream in enumerate(streams):
            extension = stream.get("ext")
            if extension not in {"mp4", "m4a", "webm", "mkv", "mov", "flv", "ts", "aac", "mp3", "ogg", "opus", "wav"}:
                raise AppException("PRESET_UNAVAILABLE", "不支持源容器", 422)
            source = directory / f"source_{index}.{extension}"
            known_size = stream.get("filesize") or stream.get("filesize_approx")
            if isinstance(known_size, (int, float)) and known_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
                raise AppException("FILE_TOO_LARGE", "源文件超过体积上限", 413)

            def progress_hook(event, index=index):
                nonlocal last_emit
                now = time.monotonic()
                if now - last_emit < 0.5 and event.get("status") != "finished":
                    return
                last_emit = now
                total = event.get("total_bytes") or event.get("total_bytes_estimate")
                ratio = min(1, event.get("downloaded_bytes", 0) / total) if total else 0
                emit({"stage": "downloading", "progress": min(85, int((index + ratio) / len(streams) * 85))})

            download_options = {**options, "skip_download": False, "simulate": False,
                                "outtmpl": str(source), "format": "best", "fixup": "never",
                                "postprocessors": [], "hls_prefer_native": True,
                                "concurrent_fragment_downloads": 1, "skip_unavailable_fragments": False,
                                "progress_hooks": [progress_hook], "overwrites": False,
                                "max_filesize": settings.MAX_FILE_SIZE_MB * 1024 * 1024}
            # Each selected stream is downloaded separately; there is no network FFmpeg fallback.
            with guarded_downloader(guard, download_options, metadata_only=False) as downloader:
                single = {**info, **stream, "formats": [stream]}
                for key in ("requested_formats", "requested_downloads", "entries"):
                    single.pop(key, None)
                downloader.process_ie_result(single, download=True)
            if guard.violation:
                raise guard.violation
            if source.is_symlink() or not source.is_file():
                raise AppException("DOWNLOAD_FAILED", "源媒体下载失败", 502)
            sources.append(source)
        emit({"stage": "processing", "progress": 90})
        extension = "mp3" if payload["preset_id"] == "audio_mp3" else "mp4"
        output = directory / f"output.{extension}"
        remaining = settings.DOWNLOAD_TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise AppException("DOWNLOAD_TIMEOUT", "下载超时", 504)
        subprocess.run(ffmpeg_command(sources, streams, output, ffmpeg), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining, check=True)
        emit({"stage": "processing", "progress": 99, "output": output.name})


def main():
    protocol_stdout = sys.stdout

    def emit(event):
        print(json.dumps(event), file=protocol_stdout, flush=True)

    try:
        with redirect_stdout(sys.stderr):
            download(json.loads(sys.argv[1]), Path(sys.argv[2]), emit)
    except Exception as exc:
        code = exc.code if isinstance(exc, AppException) else "DOWNLOAD_TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "DOWNLOAD_FAILED"
        emit({"error_code": code})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
