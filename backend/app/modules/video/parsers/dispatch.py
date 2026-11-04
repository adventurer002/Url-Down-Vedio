"""Shared source contract for the dedicated platform backends."""
import time
from typing import Any, Protocol

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers.bilibili import BilibiliSourceBackend, SourceSchemaChanged, positive
from app.modules.video.parsers.links import platform_for
from app.modules.video.parsers.youtube import YoutubeSourceBackend
from app.modules.video.parsers.douyin import DouyinSourceBackend


class PlatformSourceBackend(Protocol):
    platform: str
    def extract(self, downloader: Any, url: str) -> dict: ...
    def prepare_subtitle(self, downloader: Any, track: dict) -> dict: ...


class YtDlpSourceBackend:
    def __init__(self, platform: str):
        self.platform = platform

    def extract(self, downloader, url: str) -> dict:
        info = downloader.extract_info(url, download=False)
        actual = str(info.get("extractor_key") or info.get("extractor") or "").split(":")[0].lower()
        if self.platform != "other" and not (actual == self.platform or actual.startswith(self.platform + ":")
                or (self.platform == "bilibili" and actual.startswith("bilibili"))):
            raise AppException("VIDEO_ID_CHANGED", "解析结果与请求平台不一致", 422)
        return info

    def prepare_subtitle(self, downloader, track):
        return track


class BilibiliBackend(BilibiliSourceBackend):
    def extract(self, downloader, url):
        started = time.monotonic()
        # Bound the primary plus one compatibility attempt, including redirects.
        with downloader.source_budget(16):
            try:
                return super().extract(downloader, url)
            except SourceSchemaChanged as error:
                expected = getattr(error, "expected", None)
                if not expected or time.monotonic() - started > 20:
                    raise
                original = dict(downloader.params)
                try:
                    downloader.params.update(socket_timeout=5, retries=0, extractor_retries=0)
                    result = YtDlpSourceBackend("bilibili").extract(downloader, url)
                finally:
                    downloader.params.clear()
                    downloader.params.update(original)
                duration = result.get("duration")
                if (result.get("id") != expected["id"] or not positive(duration)
                        or abs(duration - expected["duration"]) > max(2, expected["duration"] * .02)
                        or result.get("is_live") or result.get("availability") not in {None, "public", "unlisted"}):
                    raise AppException("VIDEO_ID_CHANGED", "备用来源无法确认原视频身份及完整时长", 422)
                return result


_BACKENDS: dict[str, PlatformSourceBackend] = {
    platform: YtDlpSourceBackend(platform) for platform in ("bilibili", "youtube", "douyin", "other")
}
_BILIBILI: PlatformSourceBackend = BilibiliBackend()
_YOUTUBE: PlatformSourceBackend = YoutubeSourceBackend()
_DOUYIN: PlatformSourceBackend = DouyinSourceBackend()


def source_backend(url: str) -> PlatformSourceBackend:
    if settings.VIDEO_EGRESS_MODE == "platform" and platform_for(url) == "bilibili":
        return _BILIBILI
    if settings.VIDEO_EGRESS_MODE == "platform" and platform_for(url) == "youtube":
        return _YOUTUBE
    if settings.VIDEO_EGRESS_MODE == "platform" and platform_for(url) == "douyin":
        return _DOUYIN
    return _BACKENDS[platform_for(url)]


def extract_source(downloader, url: str) -> dict:
    return source_backend(url).extract(downloader, url)


def prepare_source_subtitle(downloader, url, track):
    return source_backend(url).prepare_subtitle(downloader, track)
