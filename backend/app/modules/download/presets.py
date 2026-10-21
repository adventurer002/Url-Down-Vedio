from app.core.exceptions import AppException
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.service import _has_codec, _rank, positive_number

PRESETS = {"video_1080_mp4": 1080, "video_720_mp4": 720, "video_480_mp4": 480, "audio_mp3": None}
SAFE_PROTOCOLS = {"http", "https", "m3u8_native", "m3u8", "http_dash_segments"}


def select_streams(video: ParsedVideo, preset_id: str) -> list[dict]:
    if preset_id not in PRESETS:
        raise AppException("INVALID_PRESET", "下载档位无效", 422)
    supported = [item for item in video.formats if not item.get("has_drm")
                 and item.get("protocol", "https") in SAFE_PROTOCOLS and item.get("url")]
    audio = [item for item in supported if _has_codec(item, "acodec") and not _has_codec(item, "vcodec")]
    audio_key = lambda item: (str(item.get("acodec", "")).startswith(("mp4a", "aac")),
                              positive_number(item.get("abr")) or positive_number(item.get("tbr")) or 0)
    if preset_id == "audio_mp3":
        candidates = audio or [item for item in supported if _has_codec(item, "acodec")]
        if candidates:
            return [max(candidates, key=audio_key)]
    else:
        candidates = [item for item in supported if item.get("height") == PRESETS[preset_id]
                      and _has_codec(item, "vcodec") and (_has_codec(item, "acodec") or audio)]
        if candidates:
            video_stream = max(candidates, key=_rank)
            return [video_stream] if _has_codec(video_stream, "acodec") else [video_stream, max(audio, key=audio_key)]
    raise AppException("PRESET_UNAVAILABLE", "当前视频不提供所选下载档位", 422)
