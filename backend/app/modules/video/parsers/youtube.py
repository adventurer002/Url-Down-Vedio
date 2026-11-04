"""YouTube-only source contract, using the locked guarded yt-dlp kernel."""
from urllib.parse import parse_qs, urlsplit

from app.core.exceptions import AppException
from app.modules.video.parsers.links import canonical_link


class YoutubeSourceBackend:
    platform = "youtube"

    def extract(self, downloader, url):
        from app.modules.video.parsers.ytdlp import map_extraction_error, source_failure

        canonical = canonical_link(url)
        parts = urlsplit(canonical)
        identifier = parse_qs(parts.query).get("v", [None])[0]
        if parts.hostname != "www.youtube.com" or parts.path != "/watch" or not identifier:
            raise AppException("UNSUPPORTED_URL", "仅支持单个公开免登录的 YouTube 视频", 422)
        try:
            # Includes all SDK client attempts and redirects, released before media.
            with downloader.source_budget(32):
                info = downloader.extract_info(canonical, download=False)
        except Exception as cause:
            error = map_extraction_error(cause)
            if failure := source_failure(cause):
                error.source_failure = failure
            raise error from None
        if not isinstance(info, dict) or info.get("_type", "video") != "video":
            raise AppException("UNSUPPORTED_URL", "仅支持单个公开视频", 422)
        actual = str(info.get("extractor_key") or info.get("extractor") or "").split(":")[0].lower()
        if actual != "youtube" or info.get("id") != identifier:
            raise AppException("VIDEO_ID_CHANGED", "YouTube 返回的视频身份不一致", 422)
        if (info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
                or info.get("availability") not in {None, "public", "unlisted"}):
            raise AppException("UNSUPPORTED_URL", "仅支持已发布、非直播的公开免登录视频", 422)
        formats = info.get("formats") or []
        if not isinstance(formats, list) or len(formats) > 2048 or any(not isinstance(item, dict) for item in formats):
            raise AppException("SOURCE_FORMATS_UNAVAILABLE", "YouTube 格式数据无效", 502)
        # Do not advertise storyboards, DRM, or unsupported containers as media.
        formats = [item for item in formats if not item.get("has_drm") and item.get("url")
                   and item.get("ext") in {"mp4", "m4a", "webm"}
                   and item.get("protocol", "https") in {"http", "https", "http_dash_segments", "m3u8_native", "m3u8"}]
        return {**info, "formats": formats}

    def prepare_subtitle(self, downloader, track):
        return track
