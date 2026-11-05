"""Public Douyin sources inside the existing watched child process.

Operators select the native share page or isolated anonymous browser backend.
"""
import json
import hashlib
import math
import re
import time
from urllib.parse import unquote, urlsplit

from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError

from app.core.exceptions import AppException
from app.core.security import normalize_url
from app.modules.video.parsers.links import canonical_link

HEADERS = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
           "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1", "Referer": "https://www.douyin.com/"}
MEDIA_DOMAINS = ("douyinvod.com", "douyin.com", "iesdouyin.com", "snssdk.com", "amemv.com",
                 "ixigua.com", "bytecdn.cn", "byteimg.com", "pstatp.com")
PAGE_LIMIT = 3 * 1024 * 1024


class ShareDataUnavailable(AppException):
    def __init__(self):
        super().__init__("SOURCE_FORMATS_UNAVAILABLE", "抖音分享页未提供可验证的公开视频数据，可改用上传文件", 502)


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def video_reference(url):
    canonical = canonical_link(url)
    parts = urlsplit(canonical)
    match = re.fullmatch(r"/video/([1-9][0-9]{0,23})", parts.path)
    if parts.hostname != "www.douyin.com" or parts.port is not None or not match:
        raise AppException("UNSUPPORTED_URL", "抖音仅支持单个公开免登录视频，请提供视频链接或上传文件", 422)
    return canonical, match[1]


def restricted(rate=False):
    error = AppException("SOURCE_RATE_LIMITED" if rate else "SOURCE_ACCESS_RESTRICTED",
                         "抖音暂时限制了服务器访问，请稍后重试或上传文件", 503)
    error.source_failure = "rate_limited" if rate else "restricted"
    return error


def read_share_page(downloader, identifier):
    deadline = time.monotonic() + 8
    request = Request(f"https://www.iesdouyin.com/share/video/{identifier}/", headers=HEADERS,
                      extensions={"timeout": 5})
    try:
        with downloader.urlopen(request) as response:
            body = bytearray()
            while chunk := response.read(min(65536, PAGE_LIMIT + 1 - len(body))):
                if time.monotonic() >= deadline:
                    raise AppException("PARSE_TIMEOUT", "抖音分享页读取超过执行时限", 504)
                body.extend(chunk)
                if len(body) > PAGE_LIMIT:
                    raise AppException("SOURCE_RESPONSE_TOO_LARGE", "抖音分享页超过大小限制", 502)
        return body.decode("utf-8")
    except HTTPError as error:
        status = error.status
        error.close()
        if status in {412, 429}:
            raise restricted(status == 429) from None
        if status == 403:
            raise AppException("SOURCE_ACCESS_RESTRICTED", "抖音分享页拒绝服务器访问，可改用上传文件", 503) from None
        if status in {404, 410}:
            raise AppException("UNSUPPORTED_URL", "抖音视频已不可用", 422) from None
        raise AppException("SOURCE_NETWORK_UNAVAILABLE", "抖音分享页暂时无法访问", 502) from None
    except (RequestError, OSError, TimeoutError):
        raise AppException("SOURCE_NETWORK_UNAVAILABLE", "抖音分享页连接失败或超时", 502) from None
    except UnicodeError:
        raise ShareDataUnavailable() from None


def share_detail(page, identifier):
    # Parse JSON only. Never evaluate JavaScript or search unrelated recommendations.
    router = re.search(r"(?:window\.)?_ROUTER_DATA\s*=\s*", page)
    render = re.search(r'<script\b[^>]*\bid=["\']RENDER_DATA["\'][^>]*>(.*?)</script\s*>', page, re.S)
    try:
        if router:
            payload, _ = json.JSONDecoder().raw_decode(page[router.end():])
            data = payload["loaderData"]["video_(id)/page"]
            if data.get("itemId") and data["itemId"] != identifier:
                raise AppException("VIDEO_ID_CHANGED", "抖音分享页的视频身份不一致", 422)
            result = data["videoInfoRes"]
        elif render:
            result = json.loads(unquote(render[1]))
        else:
            raise ShareDataUnavailable()
        if result.get("status_code", 0) != 0:
            raise AppException("UNSUPPORTED_URL", "抖音视频不在公开免登录范围内或已不可用", 422)
        items = result["item_list"]
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ShareDataUnavailable()
        return items[0]
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise ShareDataUnavailable() from None


def media_url(value):
    normalized, host, port = normalize_url(value)
    if port not in {80, 443} or not any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS):
        raise AppException("UNSUPPORTED_URL", "抖音返回了不支持的媒体域名", 422)
    return normalized


def info_from_detail(detail, identifier):
    if str(detail.get("aweme_id")) != identifier:
        raise AppException("VIDEO_ID_CHANGED", "抖音返回的视频身份不一致", 422)
    status = detail.get("status") or {}
    if (detail.get("aweme_type") not in (0, 4) or detail.get("is_live") or detail.get("is_preview")
            or detail.get("images") or detail.get("is_private") or detail.get("is_pay")
            or not isinstance(status, dict) or status.get("is_private") or status.get("is_delete")
            or status.get("private_status", 0) != 0 or status.get("is_prohibited")):
        raise AppException("UNSUPPORTED_URL", "仅支持已发布的公开免登录抖音视频", 422)
    video = detail.get("video")
    if not isinstance(video, dict):
        raise ShareDataUnavailable()
    if video.get("has_drm") or video.get("is_drm"):
        raise AppException("UNSUPPORTED_URL", "不支持受保护的抖音媒体", 422)
    duration = video.get("duration")
    if not positive(duration):
        raise ShareDataUnavailable()
    declared = detail.get("duration")
    if positive(declared) and abs(declared - duration) > max(2000, declared * .02):
        raise AppException("UNSUPPORTED_URL", "无法确认抖音视频完整时长", 422)
    rates = video.get("bit_rate") or []
    if not isinstance(rates, list) or len(rates) > 128 or any(not isinstance(item, dict) for item in rates):
        raise ShareDataUnavailable()
    formats = []
    from app.core.config import settings
    for index, item in enumerate([*rates, video]):
        address = item.get("play_addr")
        if not isinstance(address, dict) or item.get("has_drm") or item.get("is_drm"):
            continue
        urls = address.get("url_list")
        if not isinstance(urls, list) or not urls or len(urls) > 16:
            raise ShareDataUnavailable()
        width, height = address.get("width") or video.get("width"), address.get("height") or video.get("height")
        if not positive(width) or not positive(height) or max(width, height) > 16384:
            raise ShareDataUnavailable()
        stream = {"format_id": f"douyin-{index}", "url": media_url(urls[0]), "ext": "mp4", "protocol": "https",
                  "width": int(width), "height": int(height), "vcodec": "hevc" if item.get("is_h265") or item.get("is_bytevc1") else "unknown",
                  "acodec": "none" if video.get("has_audio") is False else "unknown", "http_headers": dict(HEADERS)}
        if (item.get("format") or video.get("format")) == "dash":
            stream["acodec"] = "none"
        if settings.DOUYIN_SOURCE_BACKEND == "browser":
            if stream["vcodec"] == "unknown" and re.search(r"_h264_", str(address.get("url_key", ""))):
                stream["vcodec"] = "h264"
            identity = [item.get("gear_name"), item.get("quality_type"), item.get("format"),
                        stream["width"], stream["height"], stream["vcodec"], item.get("bit_rate")]
            stream["format_id"] = "douyin-video-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]
        if positive(address.get("data_size")):
            stream["filesize"] = int(address["data_size"])
        if positive(item.get("bit_rate")):
            stream["tbr"] = item["bit_rate"] / 1000
        formats.append(stream)
    audio_rates = video.get("bit_rate_audio") or []
    if not isinstance(audio_rates, list) or len(audio_rates) > 32:
        raise ShareDataUnavailable()
    for item in audio_rates:
        if not isinstance(item, dict) or not isinstance(item.get("audio_meta"), dict):
            raise ShareDataUnavailable()
        audio = item["audio_meta"]
        addresses = audio.get("url_list") or {}
        if audio.get("media_type") != "audio" or not isinstance(addresses, dict) or not addresses.get("main_url"):
            continue
        if audio.get("has_drm") or audio.get("is_drm"):
            continue
        identity = [audio.get("file_id"), audio.get("file_hash"), audio.get("bitrate"), audio.get("size")]
        formats.append({"format_id": "douyin-audio-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20],
            "url": media_url(addresses["main_url"]), "ext": "m4a", "protocol": "https", "vcodec": "none",
            "acodec": "unknown", "filesize": int(audio["size"]) if positive(audio.get("size")) else None,
            "abr": audio["bitrate"] / 1000 if positive(audio.get("bitrate")) else None, "http_headers": dict(HEADERS)})
    if not formats:
        raise ShareDataUnavailable()
    author = detail.get("author") or {}
    return {"id": identifier, "extractor_key": "Douyin", "title": detail.get("desc") or identifier,
            "duration": duration / 1000, "uploader": author.get("nickname") if isinstance(author, dict) else None,
            "availability": "public", "formats": formats, "subtitles": {}, "automatic_captions": {}}


class DouyinSourceBackend:
    platform = "douyin"

    def extract(self, downloader, url):
        from app.modules.video.parsers.ytdlp import map_extraction_error, source_failure

        canonical, identifier = video_reference(url)
        from app.core.config import settings
        if settings.DOUYIN_SOURCE_BACKEND == "browser":
            from app.modules.video.parsers.douyin_browser import browser_detail
            return info_from_detail(browser_detail(identifier), identifier)
        # Includes the native page, SDK fallback and all their redirects. The
        # existing guard caps this phase at 26 seconds and each socket at 5 seconds.
        with downloader.source_budget(12):
            try:
                return info_from_detail(share_detail(read_share_page(downloader, identifier), identifier), identifier)
            except ShareDataUnavailable:
                original = dict(downloader.params)
                try:
                    downloader.params.update(retries=0, extractor_retries=0, socket_timeout=5)
                    info = downloader.extract_info(canonical, download=False)
                except Exception as cause:
                    if "fresh cookies" in str(cause).lower():
                        raise restricted() from None
                    error = map_extraction_error(cause)
                    if failure := source_failure(cause):
                        error.source_failure = failure
                    raise error from None
                finally:
                    downloader.params.clear()
                    downloader.params.update(original)
        if not isinstance(info, dict) or info.get("_type", "video") != "video":
            raise AppException("UNSUPPORTED_URL", "仅支持单个公开视频", 422)
        if str(info.get("id")) != identifier or str(info.get("extractor_key") or info.get("extractor") or "").lower() != "douyin":
            raise AppException("VIDEO_ID_CHANGED", "抖音备用来源的视频身份不一致", 422)
        if (info.get("is_live") or info.get("is_preview") or info.get("is_trial") or info.get("is_pay")
                or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
                or info.get("availability") not in {None, "public", "unlisted"} or not positive(info.get("duration"))):
            raise AppException("UNSUPPORTED_URL", "仅支持完整、公开免登录的抖音视频", 422)
        formats = info.get("formats") or []
        if not isinstance(formats, list) or len(formats) > 512 or any(not isinstance(item, dict) for item in formats):
            raise ShareDataUnavailable()
        valid = []
        for item in formats:
            if item.get("has_drm") or not item.get("url") or item.get("ext") not in {"mp4", "m4a"}:
                continue
            if item.get("protocol", "https") not in {"http", "https"}:
                continue
            valid.append({**item, "url": media_url(item["url"]), "http_headers": dict(HEADERS)})
        if not valid:
            raise ShareDataUnavailable()
        return {**info, "formats": valid, "http_headers": dict(HEADERS), "subtitles": {}, "automatic_captions": {}}

    def prepare_subtitle(self, downloader, track):
        return track
