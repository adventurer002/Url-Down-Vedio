"""Private metadata subprocess. All networking is routed through guarded urllib.

Uses yt-dlp networking extension points tested against the locked yt-dlp release.
The socket patch lives only in this dedicated process, never in FastAPI.
"""

from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
import json
import socket
import sys
import time
import urllib.request
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request
from yt_dlp.networking._urllib import RedirectHandler, UrllibRH

from app.core.exceptions import AppException
from app.core.config import settings
from app.modules.video.parsers.dispatch import extract_source
from app.modules.video.parsers.links import platform_for
from app.modules.video.parsers.runtime import server_proxy
from app.modules.video.parsers.youtube_dns import PublicDnsResolver
from app.core.security import check_dns_results, normalize_url
from app.modules.video.parsers.ytdlp import extraction_options, map_extraction_error, metadata_from_info, source_failure


class NetworkGuard:
    def __init__(self, proxy: str | None = None):
        self.resolve = socket.getaddrinfo
        self.violation: AppException | None = None
        self.proxy_address = None
        self.platform = None
        self.request_limit = None
        self.request_count = 0
        self.request_deadline = None
        self.public_dns: PublicDnsResolver | None = None
        if proxy:
            parts = urlsplit(proxy)
            if parts.scheme not in {"http", "socks4", "socks4a", "socks5", "socks5h"} or not parts.hostname:
                raise AppException("PARSE_FAILED", "服务端代理配置无效", 502)
            self.proxy_address = (parts.hostname.lower(), parts.port or (80 if parts.scheme == "http" else 1080))
            if settings.VIDEO_EGRESS_MODE == "platform" and settings.YOUTUBE_DNS_MODE == "proxy_doh":
                from app.modules.video.parsers.youtube_dns import ProxiedDnsResolver
                self.public_dns = ProxiedDnsResolver(proxy, self.resolve, parts.hostname,
                    deadline=lambda: self.request_deadline)
                self.resolve = self.public_dns.getaddrinfo

    def check_url(self, url: str) -> None:
        try:
            _, host, port = normalize_url(url)
            target_platform = platform_for(url)
            if self.platform and target_platform != "other" and target_platform != self.platform:
                raise AppException("UNSUPPORTED_URL", "取源请求不能跨平台更换网络策略", 422)
            check_dns_results(self.resolve(host, port, type=socket.SOCK_STREAM))
        except AppException as exc:
            self.violation = exc
            raise
        except OSError:
            raise AppException("INVALID_URL", "目标域名无法解析") from None

    def getaddrinfo(self, host, port, *args, **kwargs):
        results = self.resolve(host, port, *args, **kwargs)
        # Only the explicitly configured server proxy may be on a private network.
        # Target URLs are always checked separately, including redirects.
        host_text = host.decode() if isinstance(host, bytes) else host
        if (str(host_text).lower(), int(port)) != self.proxy_address:
            try:
                check_dns_results(results)
            except AppException as exc:
                self.violation = exc
                raise
        return results

    @contextmanager
    def request_budget(self, limit):
        previous, previous_deadline = self.request_limit, self.request_deadline
        previous_dns_limit = self.public_dns.request_limit if self.public_dns else None
        if self.public_dns:
            self.public_dns.request_limit = self.public_dns.requests + 32
        self.request_limit = self.request_count + limit
        self.request_deadline = time.monotonic() + 26
        try:
            yield
        finally:
            self.request_limit = previous
            self.request_deadline = previous_deadline
            if self.public_dns:
                self.public_dns.request_limit = previous_dns_limit

    def before_request(self):
        if self.request_deadline is not None and time.monotonic() >= self.request_deadline:
            raise AppException("PARSE_TIMEOUT", "取源超过总请求预算", 504)
        if self.request_limit is not None and self.request_count >= self.request_limit:
            raise AppException("PARSE_FAILED", "取源达到请求次数上限", 502)
        self.request_count += 1

    @contextmanager
    def installed(self):
        socket.getaddrinfo = self.getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = self.public_dns.original if self.public_dns else self.resolve
            if self.public_dns:
                self.public_dns.close()


def guarded_downloader(guard: NetworkGuard, options: dict, *, metadata_only: bool = True):
    class PublicRedirectHandler(RedirectHandler):
        max_redirections = 5
        def redirect_request(self, request, fp, code, message, headers, new_url):
            guard.check_url(new_url)
            return super().redirect_request(request, fp, code, message, headers, new_url)

    class CheckTarget(urllib.request.BaseHandler):
        handler_order = 50

        def http_request(self, request):
            getattr(guard, "before_request", lambda: None)()
            guard.check_url(request.full_url)
            return request

        https_request = http_request

    class PublicUrllibRH(UrllibRH):
        _SUPPORTED_URL_SCHEMES = ("http", "https")

        def _create_instance(self, **kwargs):
            original = super()._create_instance(**kwargs)
            opener = urllib.request.OpenerDirector()
            for handler in original.handlers:
                if isinstance(handler, urllib.request.HTTPRedirectHandler):
                    handler = PublicRedirectHandler()
                elif isinstance(handler, (urllib.request.FTPHandler, urllib.request.FileHandler, urllib.request.DataHandler)):
                    continue
                opener.add_handler(handler)
            opener.add_handler(CheckTarget())
            opener.addheaders = []
            return opener

    class MetadataYoutubeDL(YoutubeDL):
        def source_budget(self, limit):
            return guard.request_budget(limit)

        def build_request_director(self, handlers, preferences=None):
            # No requests/curl/FTP/WebSocket handler can bypass this policy.
            return super().build_request_director([PublicUrllibRH])

        def urlopen(self, request):
            deadline = getattr(guard, "request_deadline", None)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppException("PARSE_TIMEOUT", "取源超过总请求预算", 504)
                if isinstance(request, str):
                    request = Request(request)
                request.extensions["timeout"] = min(5, remaining, request.extensions.get("timeout", 5))
            url = request if isinstance(request, str) else getattr(request, "url", None) or request.full_url
            guard.check_url(url)
            return super().urlopen(request)

        def process_ie_result(self, result, download=True, extra_info=None):
            if result.get("_type", "video") not in {"video", "url", "url_transparent"}:
                raise AppException("UNSUPPORTED_URL", "仅支持单个公开视频", 422)
            return super().process_ie_result(result, download=False if metadata_only else download, extra_info=extra_info)

        def process_video_result(self, info_dict, download=True):
            # Return extractor metadata without format probing, downloads or postprocessing.
            return info_dict if metadata_only else super().process_video_result(info_dict, download=download)

        def dl(self, name, info, subtitle=False, test=False):
            from yt_dlp.downloader import get_suitable_downloader
            from yt_dlp.downloader.http import HttpFD
            from yt_dlp.downloader.hls import HlsFD
            from yt_dlp.downloader.dash import DashSegmentsFD

            downloader = get_suitable_downloader(info, self.params)
            if downloader not in {HttpFD, HlsFD, DashSegmentsFD}:
                raise AppException("UNSUPPORTED_URL", "不支持该网络传输协议", 422)
            return super().dl(name, info, subtitle=subtitle, test=test)

    return MetadataYoutubeDL(options)


def extract_metadata(url: str, proxy: str | None = None) -> dict:
    guard = NetworkGuard(proxy)
    if settings.VIDEO_EGRESS_MODE == "platform":
        guard.platform = platform_for(url)
    try:
        with guard.installed():
            guard.check_url(url)
            options = extraction_options(proxy, url=url)
            options["listsubtitles"] = True
            with guarded_downloader(guard, options) as downloader:
                info = extract_source(downloader, url)
            if guard.violation:
                raise guard.violation
            return {"data": asdict(metadata_from_info(info))}
    except Exception as exc:
        error = guard.violation or map_extraction_error(exc)
        result = {"error": error.code, "source_failure": source_failure(exc) if not guard.violation else None}
        if getattr(exc, "source_stage", None) in {"session", "view", "wbi", "play", "subtitle"}:
            result["source_stage"] = exc.source_stage
        return result


def main():
    try:
        with redirect_stdout(sys.stderr):
            result = extract_metadata(sys.argv[1], server_proxy(sys.argv[1]))
    except Exception as exc:
        result = {"error": map_extraction_error(exc).code}
    # Extractor diagnostic output must never corrupt the protocol or reach clients.
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
