"""Public UGC Bilibili source backend. Runs only inside a watched child process.

Five API requests maximum, no automatic retries, no account cookies. All I/O
uses the existing guarded downloader; media bytes still use its download kernel.
"""
import hashlib
import json
import math
import re
import time
from http.cookiejar import Cookie
from urllib.parse import parse_qs, urlencode, urlsplit

from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError

from app.core.exceptions import AppException
from app.core.security import normalize_url

API = "https://api.bilibili.com"
HEADERS = {"Referer": "https://www.bilibili.com/", "User-Agent":
           "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"}
MIXIN = (46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,
         12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52)


class SourceSchemaChanged(AppException):
    def __init__(self):
        super().__init__("SOURCE_FORMATS_UNAVAILABLE", "B 站公开接口结构暂不兼容，请稍后重试或上传文件", 502)


def restricted(rate=False):
    error = AppException("SOURCE_RATE_LIMITED" if rate else "SOURCE_ACCESS_RESTRICTED",
                         "B 站暂时限制服务器访问，请稍后重试或上传文件", 503)
    error.source_failure = "rate_limited" if rate else "restricted"
    return error


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def sign_wbi(params, image, sub, timestamp=None):
    keys = [urlsplit(value).path.rsplit("/", 1)[-1].split(".")[0] for value in (image, sub)]
    if any(not re.fullmatch(r"[0-9a-fA-F]{32}", key) for key in keys):
        raise SourceSchemaChanged()
    key = "".join((keys[0] + keys[1])[index] for index in MIXIN)[:32]
    signed = {**params, "wts": int(time.time()) if timestamp is None else timestamp}
    signed = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(signed.items())}
    signed["w_rid"] = hashlib.md5((urlencode(signed) + key).encode()).hexdigest()
    return signed


def video_reference(url):
    normalized, host, port = normalize_url(url)
    path = urlsplit(normalized).path
    if (host not in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"} or port not in {80, 443}
            or not (match := re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)(?:/)?", path))):
        raise AppException("UNSUPPORTED_URL", "B 站专用解析仅支持单个公开 BV/av 视频", 422)
    values = parse_qs(urlsplit(normalized).query, keep_blank_values=True).get("p", [])
    if values and (len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,3}", values[0])):
        raise AppException("INVALID_URL", "B 站分 P 参数无效或重复", 400)
    identifier = match[1]
    return ({"bvid": identifier} if identifier.startswith("BV") else {"aid": identifier[2:]}), (int(values[0]) if values else None)


class BilibiliClient:
    def __init__(self, downloader, *, timeout=18, max_requests=5):
        self.downloader = downloader
        self.deadline = time.monotonic() + timeout
        self.max_requests, self.requests = max_requests, 0
        self.expected = None
        self.stage = None

    def json(self, url, params=None, *, limit=2 * 1024 * 1024):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AppException("PARSE_TIMEOUT", "B 站取源超过执行时限", 504)
        if self.requests >= self.max_requests:
            raise AppException("PARSE_FAILED", "B 站取源达到请求次数上限", 502)
        self.requests += 1
        try:
            request = Request(url, query=params, headers=HEADERS,
                              extensions={"timeout": min(5, remaining)})
            with self.downloader.urlopen(request) as response:
                data = bytearray()
                while True:
                    if time.monotonic() >= self.deadline:
                        raise AppException("PARSE_TIMEOUT", "B 站取源超过执行时限", 504)
                    chunk = response.read(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        raise AppException("SOURCE_RESPONSE_TOO_LARGE", "B 站接口响应超过大小限制", 502)
            result = json.loads(data)
            if not isinstance(result, dict):
                raise SourceSchemaChanged()
            return result
        except HTTPError as error:
            status = error.status
            error.close()
            if status in {412, 429}:
                raise restricted(status == 429) from None
            if status == 404:
                raise AppException("UNSUPPORTED_URL", "视频或字幕不存在", 422) from None
            raise AppException("SOURCE_NETWORK_UNAVAILABLE", "B 站接口暂时无法访问", 502) from None
        except (TimeoutError, RequestError, OSError):
            raise AppException("SOURCE_NETWORK_UNAVAILABLE", "B 站接口连接失败或超时", 502) from None
        except (ValueError, UnicodeError):
            raise SourceSchemaChanged() from None

    def api(self, path, params=None, *, nav=False):
        self.stage = {"/x/frontend/finger/spi": "session", "/x/web-interface/view": "view",
                      "/x/web-interface/nav": "wbi", "/x/player/wbi/playurl": "play",
                      "/x/player/wbi/v2": "subtitle"}[path]
        result = self.json(API + path, params)
        code = result.get("code")
        if code in {-352, -412}:
            raise restricted()
        if code in {-799, -509}:
            raise restricted(True)
        # nav returns -101 for anonymous sessions but includes public WBI keys.
        if code != 0 and not (nav and code == -101):
            if code in {-400, -403, -404, -101, -104, 62002, 62004, 62012}:
                raise AppException("UNSUPPORTED_URL", "视频不在公开免登录范围内或已不可用", 422)
            if code is None:
                raise SourceSchemaChanged()
            raise AppException("PARSE_FAILED", "B 站公开接口暂时不可用", 502)
        if not isinstance(result.get("data"), dict):
            raise SourceSchemaChanged()
        return result["data"]

    def anonymous_session(self):
        data = self.api("/x/frontend/finger/spi")
        for source, name in (("b_3", "buvid3"), ("b_4", "buvid4")):
            value = data.get(source)
            if source == "b_4" and not value:
                continue  # buvid3 is sufficient when the anonymous endpoint omits buvid4.
            if not isinstance(value, str) or not re.fullmatch(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,512}", value):
                raise SourceSchemaChanged()
            # Host-only anonymous identifiers, never attached to media CDN URLs.
            self.downloader.cookiejar.set_cookie(Cookie(0, name, value, None, False,
                "api.bilibili.com", False, False, "/", True, True, None, True, None, None, {}))


def media_url(value, *, subtitle=False):
    if isinstance(value, str) and value.startswith("//"):
        value = "https:" + value
    normalized, host, port = normalize_url(value)
    domains = ("hdslb.com", "bilibili.com") if subtitle else ("bilivideo.com", "bilivideo.cn", "bilivideo.net", "hdslb.com")
    if port not in {80, 443} or not any(host == domain or host.endswith("." + domain) for domain in domains):
        raise AppException("UNSUPPORTED_URL", "B 站接口返回了不支持的资源域名", 422)
    return normalized


def stream_url(item):
    backups = item.get("backupUrl") or item.get("backup_url") or []
    if not isinstance(backups, list) or len(backups) > 8:
        raise SourceSchemaChanged()
    first_error = None
    for value in [item.get("baseUrl") or item.get("base_url"), *backups]:
        try:
            return media_url(value)
        except AppException as error:
            if first_error is None:
                first_error = error
    raise first_error


def formats_from_play(play, duration):
    # Reject previews and truncation instead of delivering partial paid resources.
    length = play.get("timelength")
    if play.get("is_preview") or play.get("is_trial") or not positive(length) or abs(length / 1000 - duration) > max(2, duration * .02):
        raise AppException("UNSUPPORTED_URL", "无法确认公开视频的完整时长，已拒绝试看或截断来源", 422)
    dash = play.get("dash") or {}
    formats = []
    for kind in ("audio", "video"):
        entries = dash.get(kind) or []
        if not isinstance(entries, list) or len(entries) > 128:
            raise SourceSchemaChanged()
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise SourceSchemaChanged()
            if item.get("drm_tech_type") or item.get("has_drm"):
                continue
            codec = item.get("codecs")
            if not isinstance(codec, str) or not codec:
                continue
            mime = item.get("mimeType") or item.get("mime_type")
            if mime != f"{kind}/mp4":
                continue
            # The API reorders audio variants between requests. Its quality ID
            # and codec identify the format independently of that ordering.
            stream = {"format_id": f"{kind}-{item.get('id', index)}-{codec}", "ext": "m4a" if kind == "audio" else "mp4",
                      "url": stream_url(item), "protocol": "https",
                      "vcodec": "none" if kind == "audio" else codec, "acodec": codec if kind == "audio" else "none",
                      "http_headers": dict(HEADERS)}
            if positive(item.get("bandwidth")):
                stream["tbr"] = item["bandwidth"] / 1000
                if kind == "audio":
                    stream["abr"] = stream["tbr"]
            for key in ("width", "height", "size"):
                if positive(item.get(key)):
                    stream["filesize" if key == "size" else key] = int(item[key])
            formats.append(stream)
    if not formats:
        raise SourceSchemaChanged()  # Legacy/muxed layouts may use the bounded compatibility fallback.
    return formats


class BilibiliSourceBackend:
    platform = "bilibili"

    def extract(self, downloader, url):
        reference, requested_part = video_reference(url)
        client = BilibiliClient(downloader)
        try:
            return self._extract(client, reference, requested_part)
        except (SourceSchemaChanged, KeyError, TypeError, AttributeError, IndexError) as error:
            failure = error if isinstance(error, SourceSchemaChanged) else SourceSchemaChanged()
            failure.expected = client.expected
            failure.source_stage = client.stage
            raise failure from None
        except AppException as error:
            error.source_stage = client.stage
            raise

    def _extract(self, client, reference, requested_part):
        # Establish an anonymous session before requesting video metadata too.
        client.anonymous_session()
        view = client.api("/x/web-interface/view", reference)
        bvid, aid, pages = view["bvid"], view["aid"], view["pages"]
        if (not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or not positive(aid) or not isinstance(pages, list)
                or not pages or len(pages) > 1000):
            raise SourceSchemaChanged()
        if any(not isinstance(page, dict) or page.get("page") != index for index, page in enumerate(pages, 1)):
            raise SourceSchemaChanged()
        if any(str(view.get(key)) != str(value) for key, value in reference.items()):
            raise AppException("VIDEO_ID_CHANGED", "B 站接口返回的视频身份不一致", 422)
        rights = view["rights"]
        if (view["state"] != 0 or view.get("redirect_url") or view.get("is_upower_exclusive")
                or any(rights.get(key) for key in ("is_stein_gate", "pay", "ugc_pay", "is_upower_exclusive"))):
            raise AppException("UNSUPPORTED_URL", "仅支持非交互、公开免登录的 B 站普通视频", 422)
        part = requested_part or 1
        selected = next((page for page in pages if page.get("page") == part), None)
        if selected is None:
            raise AppException("UNSUPPORTED_URL", "请求的 B 站分 P 不存在", 422)
        cid, duration = selected["cid"], selected["duration"]
        if not positive(cid) or not positive(duration):
            raise SourceSchemaChanged()
        identity = f"{bvid}_p{part}" if requested_part is not None or len(pages) > 1 else bvid
        client.expected = {"id": identity, "duration": duration}
        wbi = client.api("/x/web-interface/nav", nav=True)["wbi_img"]
        params = {"bvid": bvid, "cid": cid}
        play = client.api("/x/player/wbi/playurl", sign_wbi(
            {**params, "fnval": 4048, "fnver": 0, "fourk": 0, "qn": 64}, wbi["img_url"], wbi["sub_url"]))
        formats = formats_from_play(play, duration)
        player = client.api("/x/player/wbi/v2", sign_wbi(params, wbi["img_url"], wbi["sub_url"]))
        if (player.get("cid") is not None and str(player["cid"]) != str(cid)):
            raise AppException("VIDEO_ID_CHANGED", "B 站字幕分 P 身份不一致", 422)
        subtitles = {}
        if not player.get("need_login_subtitle"):
            tracks = (player.get("subtitle") or {}).get("subtitles") or []
            if not isinstance(tracks, list) or len(tracks) > 64:
                raise SourceSchemaChanged()
            for track in tracks:
                language = track.get("lan", "")
                if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", language) and track.get("subtitle_url"):
                    subtitles.setdefault(language, []).append({"ext": "bilibili_json",
                        "url": media_url(track["subtitle_url"], subtitle=True)})
        title = view.get("title") or bvid
        if len(pages) > 1:
            title += f" p{part:02d} {selected.get('part') or ''}"
        return {"id": identity, "extractor_key": "BiliBili", "title": title, "duration": duration,
                "thumbnail": view.get("pic"), "uploader": (view.get("owner") or {}).get("name"),
                "http_headers": dict(HEADERS), "formats": formats, "subtitles": subtitles}

    def prepare_subtitle(self, downloader, track):
        if track.get("ext") != "bilibili_json":
            return track
        data = BilibiliClient(downloader, timeout=8, max_requests=1).json(
            media_url(track["url"], subtitle=True), limit=5 * 1024 * 1024)
        body = data.get("body")
        if not isinstance(body, list) or len(body) > 30000:
            raise AppException("SUBTITLE_INVALID", "B 站字幕结构无效", 502)
        events = []
        for item in body:
            if not isinstance(item, dict):
                raise AppException("SUBTITLE_INVALID", "B 站字幕条目无效", 502)
            start, end, content = item.get("from"), item.get("to"), item.get("content")
            if (not isinstance(start, (int, float)) or isinstance(start, bool) or not math.isfinite(start) or start < 0
                    or not positive(end) or end < start or not isinstance(content, str)):
                raise AppException("SUBTITLE_INVALID", "B 站字幕时间轴无效", 502)
            events.append({"tStartMs": round(start * 1000), "dDurationMs": round((end - start) * 1000),
                           "segs": [{"utf8": content}]})
        content = json.dumps({"events": events}, ensure_ascii=False)
        if len(content.encode()) > 5 * 1024 * 1024:
            raise AppException("SUBTITLE_TOO_LARGE", "B 站字幕超过大小限制", 413)
        return {"ext": "json3", "data": content}
