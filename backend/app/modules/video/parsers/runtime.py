"""Server-owned yt-dlp configuration shared by all isolated child processes."""
from importlib.metadata import PackageNotFoundError, version
import shutil
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers.links import platform_for


def server_proxy(url: str | None = None) -> str | None:
    if settings.VIDEO_EGRESS_MODE == "platform":
        if url is None:
            raise AppException("SOURCE_EGRESS_UNAVAILABLE", "平台出口检查需要指定视频链接", 503)
        if platform_for(url) != "youtube":
            return None
        value = settings.YOUTUBE_PROXY.get_secret_value().strip()
        if not value:
            raise AppException("SOURCE_EGRESS_UNAVAILABLE", "服务器尚未配置独立的 YouTube 出口，可改用上传文件", 503)
    else:
        value = settings.YTDLP_PROXY.get_secret_value().strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "socks4", "socks4a", "socks5", "socks5h"}
                or not parts.hostname or parts.port == 0 or parts.path not in {"", "/"}
                or parts.query or parts.fragment or any(char.isspace() for char in value)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise AppException("YTDLP_PROXY_INVALID", "服务器视频网络出口配置无效，请联系管理员", 503) from None
    return value


def javascript_runtime_options(url: str | None = None) -> dict:
    if settings.VIDEO_EGRESS_MODE == "platform":
        if url is not None and platform_for(url) != "youtube":
            return {}
        if settings.YTDLP_JS_RUNTIME == "disabled":
            raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "YouTube 解析需要启用服务器 Deno / EJS 运行时", 503)
    if settings.YTDLP_JS_RUNTIME == "disabled":
        return {}
    executable = shutil.which(settings.YTDLP_DENO_PATH or "deno")
    try:
        version("yt-dlp-ejs")
    except PackageNotFoundError:
        executable = None
    if not executable:
        raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "服务器缺少 Deno 或本地 EJS 组件，请联系管理员", 503)
    if settings.VIDEO_EGRESS_MODE == "platform":
        from app.modules.video.parsers.youtube_runtime import packaged_ejs
        packaged_ejs()
    # The official Deno provider runs without allow-net/read/run/env permissions,
    # uses --no-remote/--no-npm/--cached-only for the packaged scripts, and remains
    # in the watched child process group. Remote components stay disabled.
    return {"deno": {"path": executable}}
