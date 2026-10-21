import math

from app.core.security import validate_url
from app.modules.video.parsers.base import ParsedVideo, VideoParser
from app.modules.video.schemas import DownloadOption, VideoMetadata


def positive_number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def _size(stream: dict, duration: float | None) -> int | None:
    size = positive_number(stream.get("filesize")) or positive_number(stream.get("filesize_approx"))
    if size is not None:
        return int(size)
    bitrate = positive_number(stream.get("tbr"))
    if bitrate and duration:
        return int(bitrate * 1000 * duration / 8)
    return None


def _has_codec(stream: dict, key: str) -> bool:
    return bool(stream.get(key)) and stream[key] != "none"


def _rank(stream: dict) -> tuple:
    # Prefer H.264/AAC in MP4 for direct muxing; otherwise later download stages
    # must transcode to the advertised container. No conversion occurs here.
    compatible = str(stream.get("vcodec", "")).startswith(("avc1", "h264"))
    return (compatible, positive_number(stream.get("tbr")) or 0,
            positive_number(stream.get("filesize")) or 0, str(stream.get("format_id", "")))


def aggregate_download_options(video: ParsedVideo) -> list[DownloadOption]:
    formats = [stream for stream in video.formats
               if not stream.get("has_drm") and stream.get("protocol") != "mhtml"]
    audio = [stream for stream in formats if _has_codec(stream, "acodec") and not _has_codec(stream, "vcodec")]
    best_audio = max(audio, key=lambda stream: (
        str(stream.get("acodec", "")).startswith(("mp4a", "aac")),
        positive_number(stream.get("abr")) or positive_number(stream.get("tbr")) or 0,
    ), default=None)
    options = []
    for height in (1080, 720, 480):
        candidates = [stream for stream in formats if stream.get("height") == height and _has_codec(stream, "vcodec")
                      and (_has_codec(stream, "acodec") or best_audio is not None)]
        if not candidates:
            continue
        stream = max(candidates, key=_rank)
        size = _size(stream, video.duration)
        if not _has_codec(stream, "acodec"):
            audio_size = _size(best_audio, video.duration)
            size = size + audio_size if size is not None and audio_size is not None else None
        # Transcoding changes size unpredictably; expose no misleading source size.
        selected_audio = stream if _has_codec(stream, "acodec") else best_audio
        if (not str(stream.get("vcodec", "")).startswith(("avc1", "h264"))
                or not str(selected_audio.get("acodec", "")).startswith(("mp4a", "aac"))):
            size = None
        options.append(DownloadOption(
            id=f"video_{height}_mp4", label=f"{height}p MP4", container="mp4",
            height=height, estimated_size_bytes=size,
        ))
    if any(_has_codec(stream, "acodec") for stream in formats):
        # Planned MP3 output bitrate: 192 kbps, rather than the source audio size.
        size = int(video.duration * 192_000 / 8) if positive_number(video.duration) else None
        options.append(DownloadOption(id="audio_mp3", label="MP3 音频", container="mp3", estimated_size_bytes=size))
    return options


class VideoService:
    def __init__(self, parser: VideoParser):
        self.parser = parser

    async def parse(self, url: str) -> VideoMetadata:
        normalized = await validate_url(url)
        video = await self.parser.parse(normalized)
        return VideoMetadata(
            platform=video.platform, platform_video_id=video.platform_video_id,
            title=video.title, duration=video.duration, thumbnail=video.thumbnail,
            uploader=video.uploader, has_subtitles=bool(video.subtitle_languages),
            subtitle_languages=video.subtitle_languages, download_options=aggregate_download_options(video),
        )
