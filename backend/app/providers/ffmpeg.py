"""FFmpeg helpers: remux to mp4 when needed, cancellable via polling."""

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


class FFmpegError(Exception):
    pass


class FFmpegCancelled(Exception):
    pass


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FFmpegError("ffmpeg binary not found")
    return path


def _run_cancellable(
    cmd: list[str], should_cancel: Callable[[], bool], timeout_seconds: int
) -> None:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    started = time.monotonic()
    try:
        while proc.poll() is None:
            if should_cancel():
                raise FFmpegCancelled()
            if time.monotonic() - started > timeout_seconds:
                raise FFmpegError("ffmpeg timed out")
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited with {proc.returncode}")


def remux_to_mp4(
    src: Path, dest: Path, should_cancel: Callable[[], bool], timeout_seconds: int = 1800
) -> Path:
    """Copy-codec remux into an mp4 container. Returns dest. No-op if already mp4."""
    if src.suffix.lower() == ".mp4":
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest
    ffmpeg = find_ffmpeg()
    _run_cancellable(
        [ffmpeg, "-y", "-i", str(src), "-c", "copy", str(dest)],
        should_cancel,
        timeout_seconds,
    )
    return dest


def extract_audio(
    src: Path, dest: Path, should_cancel: Callable[[], bool], timeout_seconds: int = 1800
) -> Path:
    """Extract audio track to mp3. Raises FFmpegCancelled/FFmpegError."""
    ffmpeg = find_ffmpeg()
    _run_cancellable(
        [ffmpeg, "-y", "-i", str(src), "-vn", "-acodec", "libmp3lame", "-ab", "128k", str(dest)],
        should_cancel,
        timeout_seconds,
    )
    return dest
