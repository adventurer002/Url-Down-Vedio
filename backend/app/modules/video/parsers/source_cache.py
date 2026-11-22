"""Short-lived descriptions of reproducible anonymous Bilibili DASH streams."""
import asyncio
from dataclasses import asdict
import json
import time
from urllib.parse import parse_qs, urlsplit

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.queue.redis_client import redis_client
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.parsers.bilibili import HEADERS, media_url
from app.modules.video.service import positive_number

MAX_BYTES = 32 * 1024
FIELDS = {"format_id", "ext", "url", "protocol", "vcodec", "acodec", "http_headers", "tbr", "abr",
          "width", "height", "filesize"}


def eligible(platform):
    return (settings.DOWNLOAD_SOURCE_CACHE_ENABLED and settings.VIDEO_EGRESS_MODE == "platform"
            and platform == "bilibili")


def key(video_id):
    return f"download-source:v1:{settings.VIDEO_EGRESS_POLICY_VERSION}:bilibili:{video_id}"


def snapshot(video, now):
    if not positive_number(video.duration) or not 1 <= len(video.formats) <= 64:
        return None
    expires = now + 120
    for stream in video.formats:
        if (set(stream) - FIELDS or stream.get("http_headers") != HEADERS
                or stream.get("protocol") != "https" or stream.get("ext") not in {"mp4", "m4a"}
                or not str(stream.get("format_id", "")).startswith(("video-", "audio-"))):
            return None
        try:
            url = media_url(stream["url"])
            parts = urlsplit(url)
            if parts.scheme != "https":
                return None
            deadline = parse_qs(parts.query)["deadline"]
            if len(deadline) != 1 or not deadline[0].isascii() or not deadline[0].isdecimal() or len(deadline[0]) > 12:
                return None
            expires = min(expires, int(deadline[0]) - 15)
        except (AppException, KeyError, TypeError, ValueError):
            return None
    if expires - now < 5:
        return None
    return {"expires_at": expires, "video": asdict(video)}


async def remember(video):
    if not eligible(video.platform):
        return
    now = time.time()
    item = snapshot(video, now)
    if item is None:
        return
    raw = json.dumps(item, ensure_ascii=False).encode()
    if len(raw) > MAX_BYTES:
        return
    try:
        async with asyncio.timeout(.3):
            await redis_client.set(key(video.platform_video_id), raw, ex=int(item["expires_at"] - now))
    except (RedisError, TimeoutError):
        pass


def restore(raw, payload):
    if not raw or len(raw) > MAX_BYTES:
        return None
    try:
        item = json.loads(raw)
        instant = time.time()
        video = ParsedVideo(**item["video"])
        if (item["expires_at"] <= instant or not snapshot(video, instant)
                or video.platform != payload.get("platform") or video.platform_video_id != payload.get("platform_video_id")):
            return None
        return video
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


async def load(payload):
    if not eligible(payload.get("platform")) or payload.get("upload_id") or payload.get("_audio_only"):
        return None
    try:
        async with asyncio.timeout(.3), Redis.from_url(settings.REDIS_URL, socket_connect_timeout=.2,
                                                        socket_timeout=.2) as connection:
            raw = await connection.get(key(payload["platform_video_id"]))
        return raw if restore(raw, payload) is not None else None
    except (RedisError, TimeoutError, KeyError):
        return None
