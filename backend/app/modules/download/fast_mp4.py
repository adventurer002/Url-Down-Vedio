"""Only bypass FFmpeg when a local MP4 is already suitable for delivery."""
import shutil

from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner


def fast_start_layout(path):
    try:
        size = path.stat().st_size
        offset = 0
        kinds = []
        with path.open("rb") as stream:
            for _ in range(128):
                if offset == size:
                    break
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    return False
                length, kind = int.from_bytes(header[:4], "big"), header[4:]
                if length == 1:
                    extended = stream.read(8)
                    length = int.from_bytes(extended, "big")
                    if len(extended) != 8 or length < 16:
                        return False
                elif length == 0:
                    length = size - offset
                if length < 8 or offset + length > size:
                    return False
                kinds.append(kind)
                offset += length
        return (offset == size and kinds[0:1] == [b"ftyp"]
                and kinds.count(b"moov") == 1 and kinds.count(b"mdat") == 1
                and b"moof" not in kinds and kinds.index(b"moov") < kinds.index(b"mdat"))
    except OSError:
        return False


async def ready_mp4(source, directory, **controls):
    probe = shutil.which("ffprobe")
    if not probe or not fast_start_layout(source):
        return False
    scratch = directory / "direct-mp4-probe"
    scratch.mkdir(mode=0o700, exist_ok=True)
    commands = [probe, "-v", "error", "-protocol_whitelist", "file,pipe",
                "-show_entries", "format=format_name:stream=codec_type,codec_name,pix_fmt,codec_tag_string",
                "-of", "json=compact=1", str(source)]
    # ProcessRunner's control channel expects one compact JSON line.
    from pathlib import Path
    import sys
    try:
        reply = await ProcessRunner(5, 1024 * 1024).run(
            [sys.executable, "-m", "app.modules.download.fast_mp4", *commands], scratch,
            cwd=Path(__file__).resolve().parents[3], **controls)
    except AppException as error:
        if error.code == "DOWNLOAD_FAILED" or error.code == "DOWNLOAD_TIMEOUT":
            return False
        raise
    streams = reply.get("streams", [])
    if len(streams) != 2 or "mp4" not in reply.get("format", {}).get("format_name", "").split(","):
        return False
    codecs = {stream.get("codec_type"): stream for stream in streams}
    video = codecs.get("video", {})
    return (video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
            and video.get("codec_tag_string") == "avc1" and codecs.get("audio", {}).get("codec_name") == "aac")


if __name__ == "__main__":
    import json
    import subprocess
    import sys

    result = subprocess.run(sys.argv[1:], capture_output=True, timeout=4, check=True)
    print(json.dumps(json.loads(result.stdout), separators=(",", ":")), flush=True)
