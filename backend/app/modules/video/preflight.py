"""Operator-only production check; it neither creates tasks nor downloads media.

Run from backend/: uv run python -m app.modules.video.preflight --url <public URL>
"""
import argparse
import asyncio
from importlib.metadata import version
import json
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from app.core.exceptions import AppException
from app.core.security import validate_source_url as validate_url
from app.modules.video.parsers.base import VideoParser
from app.modules.video.parsers.runtime import javascript_runtime_options, server_proxy
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.service import aggregate_download_options
from app.core.config import settings
from app.modules.video.parsers.links import platform_for
from app.modules.video.parsers.youtube_runtime import DenoEjsCapabilities


def probe_version(executable: str, runtime: bool = False) -> str | None:
    try:
        result = subprocess.run([executable, "--version" if runtime else "-version"],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    if runtime:
        match = re.search(r"^deno (\d+)\.(\d+)\.(\d+)", result.stdout)
        if not match or tuple(map(int, match.groups())) < (2, 3, 0):
            return None
        return ".".join(match.groups())
    match = re.search(r"^ffmpeg version (\S+)", result.stdout)
    return match[1] if match else None


def ffmpeg_features(executable):
    if not executable:
        return {"fps_mode": False, "vsync": False, "render_verified": False}
    try:
        result = subprocess.run([executable, "-hide_banner", "-h", "full"], capture_output=True, text=True,
                                timeout=3, check=False)
        output = result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        output = ""
    return {
        "fps_mode": bool(re.search(r"(?m)^\s*-fps_mode(?:\[[^\]\n]+\])?\s", output)),
        "vsync": bool(re.search(r"(?m)^\s*-vsync(?:\[[^\]\n]+\])?\s", output)),
        "render_verified": False,
    }


async def check(url: str | None = None, parser: VideoParser | None = None, *, platform: str | None = None) -> dict:
    policy_url = url or {"bilibili": "https://www.bilibili.com/", "youtube": "https://www.youtube.com/",
                         "douyin": "https://www.douyin.com/"}.get(platform)
    result = {"yt_dlp_version": version("yt-dlp"), "egress": "unavailable",
              "metadata_verified": False, "download_verified": False, "checks": [],
              "egress_mode": settings.VIDEO_EGRESS_MODE, "policy_version": settings.VIDEO_EGRESS_POLICY_VERSION,
              "standalone_verified": False, "restart_verified": False}
    if policy_url:
        result["platform"] = platform_for(policy_url)
        result["dns_mode"] = settings.YOUTUBE_DNS_MODE if result["platform"] == "youtube" else "system"

    def add(name, passed, code, message):
        result["checks"].append({"name": name, "passed": passed, "code": code, "message": message})

    try:
        proxy = server_proxy(policy_url)
        if settings.VIDEO_EGRESS_MODE == "platform" and settings.YOUTUBE_DNS_MODE == "proxy_doh" and policy_url and platform_for(policy_url) == "youtube":
            from app.modules.video.parsers.youtube_dns import validate_dns_proxy
            validate_dns_proxy(proxy)
        result["egress"] = "server_proxy" if proxy else "direct"
        add("egress_configuration", True, "OK", "服务端出口配置有效；客户端无需提供代理")
    except AppException as error:
        add("egress_configuration", False, error.code, error.message)

    try:
        options = (javascript_runtime_options(policy_url) if settings.VIDEO_EGRESS_MODE == "platform"
                   else javascript_runtime_options())
        youtube = policy_url is not None and platform_for(policy_url) == "youtube"
        if not options and (not policy_url or youtube):
            raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "生产 YouTube 兼容性检查需要启用 Deno", 503)
        if options:
            if settings.VIDEO_EGRESS_MODE == "platform" and youtube:
                result.update(await DenoEjsCapabilities().check())
                add("javascript_runtime", True, "OK", "Deno / EJS 离线脚本执行通过，远程组件保持关闭")
            else:
                runtime_version = await asyncio.to_thread(probe_version, options["deno"]["path"], True)
                if not runtime_version:
                    raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "Deno 无法运行或版本低于 2.3", 503)
                result["deno_version"] = runtime_version
                result["ejs_version"] = version("yt-dlp-ejs")
                add("javascript_runtime", True, "OK", "本地 Deno / EJS 可用，远程组件保持关闭")
        else:
            add("javascript_runtime", True, "NOT_REQUIRED", "该链接的检查不要求启用 YouTube 的 Deno 运行时")
    except AppException as error:
        add("javascript_runtime", False, error.code, error.message)

    ffmpeg = shutil.which("ffmpeg")
    ffmpeg_version = await asyncio.to_thread(probe_version, ffmpeg) if ffmpeg else None
    if ffmpeg_version:
        result["ffmpeg_version"] = ffmpeg_version
    result["ffmpeg_features"] = await asyncio.to_thread(ffmpeg_features, ffmpeg)
    if not result["ffmpeg_features"]["fps_mode"]:
        result["render_notice"] = "当前切片命令所需 fps_mode 参数未验证可用；需单独验证兼容或升级，不能仅凭元数据检查开放成片"
    add("ffmpeg", bool(ffmpeg_version), "OK" if ffmpeg_version else "FFMPEG_UNAVAILABLE",
        "FFmpeg 可用" if ffmpeg_version else "服务端缺少可运行的 FFmpeg")

    if url:
        try:
            normalized = await validate_url(url)
            add("source_dns", True, "OK", "目标域名通过公网 DNS 校验")
        except AppException as error:
            add("source_dns", False, error.code,
                "服务器 DNS 返回非公网地址，请修复解析；配置代理不会跳过此校验"
                if error.code == "SSRF_BLOCKED" else error.message)
        else:
            # Configuration failures are explicit; do not waste another upstream request.
            if all(item["passed"] for item in result["checks"]):
                try:
                    video = await (parser or YtDlpParser()).parse(normalized)
                    presets = aggregate_download_options(video)
                    if not presets:
                        raise AppException("SOURCE_FORMATS_UNAVAILABLE", "未获取到可下载的音视频格式", 502)
                    result.update(metadata_verified=True, platform=video.platform,
                                  video_id=video.platform_video_id, presets=[item.id for item in presets])
                    add("metadata", True, "OK", "元数据与格式解析通过；仍需真实任务下载验收")
                except AppException as error:
                    add("metadata", False, error.code, error.message)
    result["passed"] = all(item["passed"] for item in result["checks"])
    return result


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--url", help="Optional public video for metadata-only verification")
    argument_parser.add_argument("--platform", choices=["bilibili", "youtube", "douyin", "all"],
                                 help="Check per-platform configuration; no upstream request without --url")
    args = argument_parser.parse_args()
    if args.platform == "all" or (settings.VIDEO_EGRESS_MODE == "platform" and not args.url and not args.platform):
        if args.url:
            argument_parser.error("--platform all cannot be combined with --url")
        async def all_platforms():
            checks = await asyncio.gather(*(check(platform=name) for name in ("bilibili", "youtube", "douyin")))
            return {"platforms": checks, "passed": all(item["passed"] for item in checks)}
        result = asyncio.run(all_platforms())
    else:
        if args.url and args.platform and platform_for(args.url) != args.platform:
            argument_parser.error("--platform must match --url")
        result = asyncio.run(check(args.url, platform=args.platform))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
