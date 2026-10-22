from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import re
import sys

from app.core.exceptions import AppException
from app.modules.video.parsers.ytdlp import extraction_options, metadata_from_info
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
    guard = NetworkGuard(os.environ.get("YTDLP_PROXY"))
    with guard.installed():
        guard.check_url(payload["url"])
        options = extraction_options(os.environ.get("YTDLP_PROXY"))
        # Bilibili (and some other extractors) only query subtitle tracks on request.
        # The metadata-only downloader still forbids all file/media writes.
        options["listsubtitles"] = True
        with guarded_downloader(guard, options) as parser:
            info = parser.extract_info(payload["url"], download=False)
        if guard.violation:
            raise guard.violation
        video = metadata_from_info(info)
        if (video.platform, video.platform_video_id) != (payload["platform"], payload["platform_video_id"]):
            raise AppException("VIDEO_ID_CHANGED", "视频身份发生变化", 422)
        for source in ("subtitles", "automatic_captions"):
            info[source] = {key: [track for track in tracks if track.get("ext") in {"vtt", "srt", "json3"}]
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
        emit({"error_code": exc.code if isinstance(exc, AppException) else "SUBTITLE_FETCH_FAILED"})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
