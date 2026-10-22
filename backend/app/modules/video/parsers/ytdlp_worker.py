"""Private metadata subprocess. All networking is routed through guarded urllib.

Uses yt-dlp networking extension points tested against the locked yt-dlp release.
The socket patch lives only in this dedicated process, never in FastAPI.
"""

from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
import json
import os
import socket
import sys
import urllib.request
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.networking._urllib import RedirectHandler, UrllibRH

from app.core.exceptions import AppException
from app.core.security import check_dns_results, normalize_url
from app.modules.video.parsers.ytdlp import extraction_options, map_extraction_error, metadata_from_info


class NetworkGuard:
    def __init__(self, proxy: str | None = None):
        self.resolve = socket.getaddrinfo
        self.violation: AppException | None = None
        self.proxy_address = None
        if proxy:
            parts = urlsplit(proxy)
            if parts.scheme not in {"http", "socks4", "socks4a", "socks5", "socks5h"} or not parts.hostname:
                raise AppException("PARSE_FAILED", "服务端代理配置无效", 502)
            self.proxy_address = (parts.hostname.lower(), parts.port or (80 if parts.scheme == "http" else 1080))

    def check_url(self, url: str) -> None:
        try:
            _, host, port = normalize_url(url)
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
    def installed(self):
        socket.getaddrinfo = self.getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = self.resolve


def guarded_downloader(guard: NetworkGuard, options: dict, *, metadata_only: bool = True):
    class PublicRedirectHandler(RedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            guard.check_url(new_url)
            return super().redirect_request(request, fp, code, message, headers, new_url)

    class CheckTarget(urllib.request.BaseHandler):
        handler_order = 50

        def http_request(self, request):
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
        def build_request_director(self, handlers, preferences=None):
            # No requests/curl/FTP/WebSocket handler can bypass this policy.
            return super().build_request_director([PublicUrllibRH])

        def urlopen(self, request):
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
    try:
        with guard.installed():
            guard.check_url(url)
            options = extraction_options(proxy)
            options["listsubtitles"] = True
            with guarded_downloader(guard, options) as downloader:
                info = downloader.extract_info(url, download=False)
            if guard.violation:
                raise guard.violation
            return {"data": asdict(metadata_from_info(info))}
    except Exception as exc:
        error = guard.violation or map_extraction_error(exc)
        return {"error": error.code}


def main():
    try:
        with redirect_stdout(sys.stderr):
            result = extract_metadata(sys.argv[1], os.environ.get("YTDLP_PROXY"))
    except Exception as exc:
        result = {"error": map_extraction_error(exc).code}
    # Extractor diagnostic output must never corrupt the protocol or reach clients.
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
