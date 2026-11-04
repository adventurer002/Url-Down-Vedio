import math

from app.core.security import validate_source_url as validate_url
from app.core.config import settings
from app.modules.video.parsers.links import canonical_link
from app.modules.video.parsers.operations import metadata_budget
from app.modules.video.parsers.base import ParsedVideo, VideoParser
from app.modules.video.schemas import DownloadOption, VideoMetadata

SAFE_PROTOCOLS = {"http", "https", "m3u8_native", "m3u8", "http_dash_segments"}


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


def requires_audio_only(video: ParsedVideo) -> bool:
    return settings.VIDEO_EGRESS_MODE == "platform" and video.platform == "youtube"


def audio_rank(stream: dict) -> tuple:
    return (str(stream.get("acodec", "")).startswith(("mp4a", "aac")),
            positive_number(stream.get("abr")) or positive_number(stream.get("tbr")) or 0)


def source_video_streams(video: ParsedVideo) -> list[dict]:
    """Preserve native dimensions when exact-height tiers cannot form a video."""
    if settings.VIDEO_EGRESS_MODE != "platform" or video.platform not in {"bilibili", "youtube", "douyin"}:
        return []
    supported = [item for item in video.formats if item.get("url") and not item.get("has_drm")
                 and item.get("protocol", "https") in SAFE_PROTOCOLS and item.get("ext") in {"mp4", "m4a", "webm"}]
    audio = [item for item in supported if _has_codec(item, "acodec") and not _has_codec(item, "vcodec")]
    candidates = [item for item in supported if positive_number(item.get("width"))
                  and positive_number(item.get("height")) and max(item["width"], item["height"]) <= 16384
                  and _has_codec(item, "vcodec") and (_has_codec(item, "acodec") or audio)]
    if not candidates or (video.platform != "douyin" and any(item["height"] in {1080, 720, 480} for item in candidates)):
        return []
    # Bound the preferred source to Full-HD pixel area, preserving its aspect
    # ratio. If all sources exceed that area, choose the smallest available one.
    bounded = [item for item in candidates if item["width"] * item["height"] <= 1920 * 1080]
    if bounded:
        source = max(bounded, key=lambda item: (item["width"] * item["height"], _rank(item)))
    else:
        source = max(candidates, key=lambda item: (-item["width"] * item["height"], _rank(item)))
    return [source] if _has_codec(source, "acodec") else [source, max(audio, key=audio_rank)]


def aggregate_download_options(video: ParsedVideo) -> list[DownloadOption]:
    formats = [stream for stream in video.formats
               if not stream.get("has_drm") and stream.get("protocol") != "mhtml"]
    audio = [stream for stream in formats if _has_codec(stream, "acodec") and not _has_codec(stream, "vcodec")]
    best_audio = max(audio, key=audio_rank, default=None)
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
    source_streams = source_video_streams(video)
    if source_streams:
        source, selected_audio = source_streams[0], source_streams[-1]
        sizes = [_size(item, video.duration) for item in source_streams]
        size = sum(sizes) if all(item is not None for item in sizes) else None
        if (not str(source.get("vcodec", "")).startswith(("avc1", "h264"))
                or not str(selected_audio.get("acodec", "")).startswith(("mp4a", "aac"))):
            size = None
        options.append(DownloadOption(id="video_source_mp4", label=f"原画幅 {source['width']}×{source['height']} MP4",
                                      container="mp4", height=int(source["height"]), estimated_size_bytes=size))
    audio_exportable = bool(audio) if requires_audio_only(video) else any(_has_codec(stream, "acodec") for stream in formats)
    if audio_exportable:
        # Planned MP3 output bitrate: 192 kbps, rather than the source audio size.
        size = int(video.duration * 192_000 / 8) if positive_number(video.duration) else None
        options.append(DownloadOption(id="audio_mp3", label="MP3 音频", container="mp3", estimated_size_bytes=size))
    return options


class VideoService:
    def __init__(self, parser: VideoParser):
        self.parser = parser

    async def parse(self, url: str) -> VideoMetadata:
        with metadata_budget():
            normalized = await validate_url(canonical_link(url))
            video = await self.parser.parse(normalized)
        return VideoMetadata(
            platform=video.platform, platform_video_id=video.platform_video_id,
            title=video.title, duration=video.duration, thumbnail=video.thumbnail,
            uploader=video.uploader, has_subtitles=bool(video.subtitle_languages),
            subtitle_languages=video.subtitle_languages, download_options=aggregate_download_options(video),
        )
