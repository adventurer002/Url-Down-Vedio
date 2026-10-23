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

from app.core.exceptions import AppException
from app.core.security import validate_url
from app.modules.video.parsers.base import VideoParser
from app.modules.video.parsers.runtime import javascript_runtime_options, server_proxy
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.service import aggregate_download_options


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


async def check(url: str | None = None, parser: VideoParser | None = None) -> dict:
    result = {"yt_dlp_version": version("yt-dlp"), "egress": "direct",
              "metadata_verified": False, "download_verified": False, "checks": []}

    def add(name, passed, code, message):
        result["checks"].append({"name": name, "passed": passed, "code": code, "message": message})

    try:
        proxy = server_proxy()
        result["egress"] = "server_proxy" if proxy else "direct"
        add("egress_configuration", True, "OK", "服务端出口配置有效；客户端无需提供代理")
    except AppException as error:
        add("egress_configuration", False, error.code, error.message)

    try:
        options = javascript_runtime_options()
        if not options:
            raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "生产 YouTube 兼容性检查需要启用 Deno", 503)
        runtime_version = await asyncio.to_thread(probe_version, options["deno"]["path"], True)
        if not runtime_version:
            raise AppException("YTDLP_RUNTIME_UNAVAILABLE", "Deno 无法运行或版本低于 2.3", 503)
        result["deno_version"] = runtime_version
        result["ejs_version"] = version("yt-dlp-ejs")
        add("javascript_runtime", True, "OK", "本地 Deno / EJS 可用，远程组件保持关闭")
    except AppException as error:
        add("javascript_runtime", False, error.code, error.message)

    ffmpeg = shutil.which("ffmpeg")
    ffmpeg_version = await asyncio.to_thread(probe_version, ffmpeg) if ffmpeg else None
    if ffmpeg_version:
        result["ffmpeg_version"] = ffmpeg_version
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
    args = argument_parser.parse_args()
    result = asyncio.run(check(args.url))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
