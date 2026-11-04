"""Private download process. FFmpeg children inherit this process group."""

from contextlib import contextmanager, redirect_stdout
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from yt_dlp.utils import DownloadError

from app.core.config import settings

from app.core.exceptions import AppException
from app.modules.video.parsers.runtime import server_proxy
from app.modules.video.parsers.dispatch import extract_source
from app.modules.video.parsers.links import platform_for
from app.modules.download.presets import select_streams, source_selection
from app.modules.video.service import positive_number
from app.modules.video.parsers.ytdlp import extraction_options, metadata_from_info, map_extraction_error, source_failure
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
    proxy = server_proxy(payload["url"])
    guard = NetworkGuard(proxy)
    if settings.VIDEO_EGRESS_MODE == "platform":
        guard.platform = platform_for(payload["url"])
    last_emit = 0.0
    with guard.installed(), native_network_only():
        guard.check_url(payload["url"])
        options = extraction_options(proxy, url=payload["url"])
        with guarded_downloader(guard, options) as parser:
            info = extract_source(parser, payload["url"])
        if guard.violation:
            raise guard.violation
        video = metadata_from_info(info)
        if "allowance" in payload:
            from app.modules.billing.policy import seconds
            if seconds(video.duration) > payload["allowance"]["limits"]["video_seconds"]:
                raise AppException("VIDEO_TOO_LONG", "实际视频时长超过任务限制", 422)
        if (video.platform, video.platform_video_id) != (payload["platform"], payload["platform_video_id"]):
            raise AppException("VIDEO_ID_CHANGED", "视频身份发生变化", 422)
        if payload.get("preset_id") == "video_source_mp4" and source_selection(video) != payload.get("source_selection"):
            raise AppException("SOURCE_SELECTION_CHANGED", "源画幅或格式发生变化，请重新报价", 409)
        emit({"_source_metadata_ready": True})
        if payload.get("_audio_only"):
            from app.modules.media.audio import select_audio_stream
            streams = [select_audio_stream(video)]
        else:
            streams = select_streams(video, payload["preset_id"])
        sources = []
        source_limit = min(settings.MAX_FILE_SIZE_MB * 1024 * 1024,
                           payload.get("allowance", {}).get("limits", {}).get("file_bytes", settings.MAX_FILE_SIZE_MB * 1024 * 1024),
                           payload.get("allowance", {}).get("source_selection", {}).get("max_source_bytes", settings.MAX_FILE_SIZE_MB * 1024 * 1024))
        known_total = sum(positive_number(item.get("filesize")) or positive_number(item.get("filesize_approx")) or 0
                          for item in streams)
        if known_total > source_limit:
            raise AppException("FILE_TOO_LARGE", "源文件合计超过体积上限", 413)
        downloaded_sources_bytes = 0
        for index, stream in enumerate(streams):
            extension = stream.get("ext")
            if extension not in {"mp4", "m4a", "webm", "mkv", "mov", "flv", "ts", "aac", "mp3", "ogg", "opus", "wav"}:
                raise AppException("PRESET_UNAVAILABLE", "不支持源容器", 422)
            source = directory / f"source_{index}.{extension}"
            def progress_hook(event, index=index):
                nonlocal last_emit
                downloaded = positive_number(event.get("downloaded_bytes")) or 0
                if downloaded_sources_bytes + downloaded > source_limit:
                    raise AppException("FILE_TOO_LARGE", "源文件合计超过体积上限", 413)
                now = time.monotonic()
                if now - last_emit < 0.5 and event.get("status") != "finished":
                    return
                last_emit = now
                total = positive_number(event.get("total_bytes")) or positive_number(event.get("total_bytes_estimate"))
                ratio = min(1, downloaded / total) if total else 0
                emit({"stage": "downloading", "progress": min(85, int((index + ratio) / len(streams) * 85))})

            download_options = {**options, "skip_download": False, "simulate": False,
                                "outtmpl": str(source), "format": "bestaudio/best" if payload.get("_audio_only") else "best", "fixup": "never",
                                "postprocessors": [], "hls_prefer_native": True,
                                "concurrent_fragment_downloads": 1, "skip_unavailable_fragments": False,
                                "progress_hooks": [progress_hook], "overwrites": False,
                                "max_filesize": source_limit}
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
            downloaded_sources_bytes += source.stat().st_size
            if downloaded_sources_bytes > source_limit:
                raise AppException("FILE_TOO_LARGE", "源文件合计超过体积上限", 413)
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
        if isinstance(exc, AppException):
            code = exc.code
        elif isinstance(exc, subprocess.TimeoutExpired):
            code = "DOWNLOAD_TIMEOUT"
        elif isinstance(exc, DownloadError):
            code = map_extraction_error(exc).code
        else:
            code = "DOWNLOAD_FAILED"
        emit({"error_code": code, "source_failure": source_failure(exc)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
