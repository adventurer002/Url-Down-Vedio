"""POSIX process-group watchdog, independent of Celery Solo remote control."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from app.core.exceptions import AppException


def directory_size(directory: Path) -> int:
    total = 0
    for folder, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
        for name in files:
            try:
                total += (Path(folder) / name).lstat().st_size
            except FileNotFoundError:
                pass
    return total


class ProcessRunner:
    def __init__(self, timeout: float, max_bytes: int, interval: float = 0.5, term_grace: float = 3):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.interval = interval
        self.term_grace = term_grace

    @staticmethod
    def group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # EPERM means the group still exists. macOS may report it briefly
            # while SIGKILL descendants are being reaped; keep waiting.
            return True

    async def stop_group(self, process: subprocess.Popen) -> None:
        pgid = process.pid
        if self.group_exists(pgid):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + self.term_grace
        while time.monotonic() < deadline:
            process.poll()  # Reap the group leader even if its descendants linger.
            if not self.group_exists(pgid):
                return
            await asyncio.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not self.group_exists(pgid):
                return
            await asyncio.sleep(0.05)
        raise AppException("PROCESS_CLEANUP_FAILED", "下载进程组未能确认退出", 500)

    async def run(self, command: Sequence[str], directory: Path, *, cwd: Path,
                  is_cancel_requested: Callable[[], Awaitable[bool]],
                  on_progress: Callable[[dict], Awaitable[None]],
                  check_capacity: Callable[[], None] = lambda: None) -> dict:
        process = None
        buffer = bytearray()
        last = {}
        succeeded = False
        started = time.monotonic()

        async def consume():
            nonlocal last
            # Bound both work per tick and total pending data to prevent pipe abuse.
            for _ in range(16):
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > 1024 * 1024:
                    raise AppException("DOWNLOAD_FAILED", "下载进程输出超过限制", 502)
            while b"\n" in buffer:
                line, _, remaining = buffer.partition(b"\n")
                buffer[:] = remaining
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                except (ValueError, UnicodeError):
                    continue
                if "error_code" in event:
                    code = event["error_code"]
                    messages = {"VIDEO_TOO_LONG": "实际视频时长超过任务限制", "DURATION_REQUIRED": "无法确认实际视频时长",
                                "SSRF_BLOCKED": "禁止访问非公开网络地址", "INVALID_URL": "目标 URL 无效",
                                "UNSUPPORTED_URL": "视频不受支持", "PRESET_UNAVAILABLE": "视频已不提供所选档位",
                                "DOWNLOAD_TIMEOUT": "下载或转换超时", "FILE_TOO_LARGE": "文件超过任务体积上限",
                                "FFMPEG_UNAVAILABLE": "服务端缺少 FFmpeg", "VIDEO_ID_CHANGED": "视频身份发生变化",
                                "AUDIO_STREAM_UNAVAILABLE": "没有独立音轨，已阻止下载完整视频",
                                "SUBTITLE_FETCH_FAILED": "字幕获取失败", "SUBTITLE_TOO_LARGE": "字幕文件超过大小限制",
                                "YTDLP_PROXY_INVALID": "服务器视频网络出口配置无效，请联系管理员",
                                "YTDLP_RUNTIME_UNAVAILABLE": "服务器视频解析运行时不可用，请联系管理员",
                                "SOURCE_ACCESS_RESTRICTED": "视频平台要求额外验证或限制了服务器访问，请稍后重试",
                                "SOURCE_RATE_LIMITED": "视频平台请求频率受限，请稍后重试",
                                "SOURCE_NETWORK_UNAVAILABLE": "服务器暂时无法连接视频平台，请稍后重试",
                                "PARSE_TIMEOUT": "视频平台连接或解析超时",
                                "PARSE_FAILED": "视频平台解析失败"}
                    if code not in messages:
                        code = "DOWNLOAD_FAILED"
                    status = 503 if code in {"YTDLP_PROXY_INVALID", "YTDLP_RUNTIME_UNAVAILABLE", "SOURCE_RATE_LIMITED"} else 504 if code == "PARSE_TIMEOUT" else 502
                    raise AppException(code, messages.get(code, "视频下载失败"), status)
                if isinstance(event.get("progress"), int) and 0 <= event["progress"] <= 100:
                    await on_progress(event)
                last = event

        try:
            if await is_cancel_requested():
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            process = subprocess.Popen(list(command), cwd=cwd, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            (directory / ".process.json").write_text(json.dumps({"pgid": process.pid}), encoding="utf-8")
            os.set_blocking(process.stdout.fileno(), False)
            while True:
                tick = time.monotonic()
                returncode = process.poll()
                if time.monotonic() - started >= self.timeout:
                    raise AppException("DOWNLOAD_TIMEOUT", "下载超过总执行时限", 504)
                if directory_size(directory) > self.max_bytes:
                    raise AppException("FILE_TOO_LARGE", "文件超过任务体积上限", 413)
                check_capacity()
                async with asyncio.timeout(3):
                    if await is_cancel_requested():
                        raise AppException("TASK_CANCELLED", "任务已取消", 409)
                    await consume()
                if time.monotonic() - started >= self.timeout:
                    raise AppException("DOWNLOAD_TIMEOUT", "下载超过总执行时限", 504)
                if returncode is not None:
                    if returncode != 0:
                        raise AppException("DOWNLOAD_FAILED", "下载进程异常退出", 502)
                    await self.stop_group(process)
                    succeeded = True
                    return last
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - tick)))
        except TimeoutError:
            raise AppException("DOWNLOAD_CONTROL_TIMEOUT", "下载控制通道超时", 503) from None
        finally:
            try:
                if process is not None and not succeeded:
                    await self.stop_group(process)
                if not succeeded:
                    shutil.rmtree(directory, ignore_errors=False) if directory.exists() else None
            finally:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
