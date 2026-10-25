"""Bounded, local-only audio duration probe under the existing process watchdog."""
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import sys

from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.modules.billing.policy import seconds


async def audio_seconds(path, directory, controls, max_bytes):
    reply = await ProcessRunner(15, max_bytes).run(
        [sys.executable, "-m", "app.modules.media.probe", str(path)], directory,
        cwd=Path(__file__).resolve().parents[3], **controls)
    return seconds(reply.get("audio_seconds"))


def main():
    executable = shutil.which("ffprobe")
    if not executable:
        raise AppException("FFMPEG_UNAVAILABLE", "服务端缺少 ffprobe", 503)
    result = subprocess.run([executable, "-v", "error", "-protocol_whitelist", "file,pipe,crypto",
        "-show_entries", "format=duration", "-of", "json", sys.argv[1]],
        capture_output=True, timeout=10, check=True)
    value = Decimal(json.loads(result.stdout)["format"]["duration"])
    print(json.dumps({"audio_seconds": str(value)}), flush=True)


if __name__ == "__main__":
    main()
