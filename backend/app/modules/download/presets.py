from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.service import _has_codec, _rank, requires_audio_only, source_video_streams, audio_rank, SAFE_PROTOCOLS

PRESETS = {"video_1080_mp4": 1080, "video_720_mp4": 720, "video_480_mp4": 480,
           "video_source_mp4": None, "audio_mp3": None}


def select_streams(video: ParsedVideo, preset_id: str, *, prefer_https: bool = True) -> list[dict]:
    if preset_id not in PRESETS:
        raise AppException("INVALID_PRESET", "下载档位无效", 422)
    if preset_id == "video_source_mp4":
        streams = source_video_streams(video)
        if not streams:
            raise AppException("PRESET_UNAVAILABLE", "当前视频不提供可验证的原画幅 MP4", 422)
        return streams
    supported = [item for item in video.formats if not item.get("has_drm")
                 and item.get("protocol", "https") in SAFE_PROTOCOLS and item.get("url")]
    audio = [item for item in supported if _has_codec(item, "acodec") and not _has_codec(item, "vcodec")]
    if preset_id == "audio_mp3":
        if requires_audio_only(video) and not audio:
            raise AppException("AUDIO_STREAM_UNAVAILABLE", "YouTube 没有独立音轨，可改用上传文件", 422)
        candidates = audio or [item for item in supported if _has_codec(item, "acodec")]
        if candidates:
            return [max(candidates, key=audio_rank)]
    else:
        candidates = [item for item in supported if item.get("height") == PRESETS[preset_id]
                      and _has_codec(item, "vcodec") and (_has_codec(item, "acodec") or audio)]
        if candidates:
            video_stream = max(candidates, key=_rank)
            if (prefer_https and settings.VIDEO_EGRESS_MODE == "platform" and video.platform == "youtube"
                    and video_stream.get("protocol") in {"m3u8", "m3u8_native", "http_dash_segments"}):
                # A manifest's advertised bandwidth can outrank a regular file
                # with the same display properties and add many proxy requests.
                equivalent = ("width", "height", "fps", "vcodec", "acodec", "ext")
                direct = (
                    item for item in candidates
                    if item.get("protocol", "https") == "https" and item["url"].startswith("https://")
                    and not item.get("fragments")
                    and all(item.get(key) == video_stream.get(key) for key in equivalent)
                )
                video_stream = max(direct, key=_rank, default=video_stream)
            return [video_stream] if _has_codec(video_stream, "acodec") else [video_stream, max(audio, key=audio_rank)]
    raise AppException("PRESET_UNAVAILABLE", "当前视频不提供所选下载档位", 422)


def source_selection(video: ParsedVideo) -> dict:
    streams = select_streams(video, "video_source_mp4")
    stream = streams[0]
    selection = {"format_id": stream.get("format_id"), "width": stream["width"], "height": stream["height"],
            "vcodec": stream.get("vcodec"), "acodec": stream.get("acodec"), "duration": video.duration,
            "source_mode": "muxed_video"}
    if len(streams) == 2:
        audio = streams[1]
        selection.update(source_mode="separate_audio_video", audio_format_id=audio.get("format_id"),
                         audio_acodec=audio.get("acodec"), audio_ext=audio.get("ext"),
                         video_ext=stream.get("ext"), video_protocol=stream.get("protocol", "https"),
                         audio_protocol=audio.get("protocol", "https"))
    return selection
