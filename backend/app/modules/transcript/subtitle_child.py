from contextlib import redirect_stdout
import json
from pathlib import Path
import re
import sys

from yt_dlp.utils import DownloadError

from app.core.exceptions import AppException
from app.core.config import settings
from app.modules.video.parsers.dispatch import extract_source, prepare_source_subtitle
from app.modules.video.parsers.links import platform_for
from app.modules.video.parsers.runtime import server_proxy
from app.modules.video.parsers.ytdlp import extraction_options, metadata_from_info, map_extraction_error, source_failure
from app.modules.video.parsers.ytdlp_worker import NetworkGuard, guarded_downloader


def select_language(info: dict, requested: str | None) -> str | None:
    for source in ("subtitles", "automatic_captions"):
        available = sorted(key for key, value in (info.get(source) or {}).items()
                           if value and key not in {"live_chat", "danmaku"} and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key))
        if requested:
            exact = next((key for key in available if key.lower() == requested.lower()), None)
            matches = [key for key in available if key.lower().removeprefix("ai-").split("-")[0] == requested.lower().split("-")[0]]
            if exact or matches:
                return exact or matches[0]
        elif available:
            original = info.get("language")
            return original if original in available else available[0]
    return None


def fetch(payload: dict, directory: Path, emit):
    proxy = server_proxy(payload["url"])
    guard = NetworkGuard(proxy)
    if settings.VIDEO_EGRESS_MODE == "platform":
        guard.platform = platform_for(payload["url"])
    with guard.installed():
        guard.check_url(payload["url"])
        options = extraction_options(proxy, url=payload["url"])
        # Bilibili (and some other extractors) only query subtitle tracks on request.
        # The metadata-only downloader still forbids all file/media writes.
        options["listsubtitles"] = True
        with guarded_downloader(guard, options) as parser:
            info = extract_source(parser, payload["url"])
        if guard.violation:
            raise guard.violation
        video = metadata_from_info(info)
        if (video.platform, video.platform_video_id) != (payload["platform"], payload["platform_video_id"]):
            raise AppException("VIDEO_ID_CHANGED", "视频身份发生变化", 422)
        emit({"_source_metadata_ready": True})
        for source in ("subtitles", "automatic_captions"):
            info[source] = {key: [track for track in tracks if track.get("ext") in {"vtt", "srt", "json3", "bilibili_json"}]
                            for key, tracks in (info.get(source) or {}).items()}
        selected = select_language(info, payload.get("language"))
        if selected is None:
            emit({"subtitle_found": False})
            return
        options.update(skip_download=True, simulate=False, listsubtitles=False, writesubtitles=True, writeautomaticsub=True,
                       subtitleslangs=[f"^{re.escape(selected)}$"], subtitlesformat="vtt/srt/json3",
                       outtmpl=str(directory / "subtitle.%(ext)s"), ignore_no_formats_error=True,
                       postprocessors=[], fixup="never")
        with guarded_downloader(guard, options, metadata_only=False) as downloader:
            for source in ("subtitles", "automatic_captions"):
                if selected in info[source]:
                    info[source] = {selected: [prepare_source_subtitle(downloader, payload["url"], track)
                                              for track in info[source][selected][:1]]}
            downloader.process_ie_result(info, download=True)
        if guard.violation:
            raise guard.violation
        paths = [path for path in directory.iterdir() if path.suffix in {".vtt", ".srt", ".json3"} and not path.is_symlink()]
        if not paths:
            raise AppException("SUBTITLE_FETCH_FAILED", "字幕轨存在但获取失败", 502)
        if paths[0].stat().st_size > 5 * 1024 * 1024:
            raise AppException("SUBTITLE_TOO_LARGE", "字幕文件超过上限", 413)
        emit({"subtitle_found": True, "filename": paths[0].name, "language": selected})


def main():
    output = sys.stdout

    def emit(value):
        print(json.dumps(value), file=output, flush=True)

    try:
        with redirect_stdout(sys.stderr):
            fetch(json.loads(sys.argv[1]), Path(sys.argv[2]), emit)
    except Exception as exc:
        if isinstance(exc, AppException):
            code = exc.code
        elif isinstance(exc, DownloadError):
            code = map_extraction_error(exc).code
        else:
            code = "SUBTITLE_FETCH_FAILED"
        emit({"error_code": code, "source_failure": source_failure(exc)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
