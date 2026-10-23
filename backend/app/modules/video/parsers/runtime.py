"""Server-owned yt-dlp configuration shared by all isolated child processes."""
from importlib.metadata import PackageNotFoundError, version
import shutil
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.exceptions import AppException


def server_proxy() -> str | None:
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


def javascript_runtime_options() -> dict:
    if settings.YTDLP_JS_RUNTIME == "disabled":
        return {}
    executable = shutil.which(settings.YTDLP_DENO_PATH or "deno")
    try:
        version("yt-dlp-ejs")
    except PackageNotFoundError:
        executable = None
    if not executable:
        raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "服务器缺少 Deno 或本地 EJS 组件，请联系管理员", 503)
    # The official Deno provider runs without allow-net/read/run/env permissions,
    # uses --no-remote/--no-npm/--cached-only for the packaged scripts, and remains
    # in the watched child process group. Remote components stay disabled.
    return {"deno": {"path": executable}}
