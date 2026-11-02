"""Bounded ffprobe + content hashing in the worker's supervised subprocess group."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys


def number(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def inspect(path):
    executable = shutil.which("ffprobe")
    if executable is None:
        raise ValueError("missing ffprobe")
    result = subprocess.run([executable, "-v", "error", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "mov,matroska,webm,mp3", "-probesize", "5000000", "-analyzeduration", "5000000",
        "-show_entries", "format=start_time,duration:stream=index,codec_type,codec_name,start_time,duration,time_base,width,height,avg_frame_rate:stream_disposition=attached_pic:stream_side_data=rotation",
        "-of", "json", str(path)], timeout=15, check=True, capture_output=True)
    data = json.loads(result.stdout)
    video = next(s for s in data["streams"] if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))
    audio = next(s for s in data["streams"] if s.get("codec_type") == "audio")
    origin = number(data["format"].get("start_time"), 0)
    duration = number(data["format"].get("duration"))
    video_start, audio_start = number(video.get("start_time"), origin), number(audio.get("start_time"), origin)
    ends = [origin + duration] if duration and duration > 0 else []
    for stream, start in ((video, video_start), (audio, audio_start)):
        value = number(stream.get("duration"))
        if value and value > 0:
            ends.append(start + value)
    if not ends or min(ends) <= max(audio_start, video_start):
        raise ValueError("invalid timeline")
    container_duration = duration or max(ends) - origin
    audio_duration = number(audio.get("duration"), origin + container_duration - audio_start)
    video_duration = number(video.get("duration"), origin + container_duration - video_start)
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return {"duration": min(ends) - origin, "clip_start": max(audio_start, video_start) - origin,
        "container_duration": container_duration, "resource_duration": max(ends) - origin,
        "audio_duration": audio_duration, "video_duration": video_duration,
        "audio_end": audio_start + audio_duration, "video_end": video_start + video_duration,
        "billable_audio_seconds": math.ceil(audio_duration), "origin": origin, "audio_start": audio_start,
        "video_start": video_start, "audio_index": audio["index"], "video_index": video["index"],
        "audio_time_base": audio.get("time_base"), "video_time_base": video.get("time_base"),
        "width": video.get("width"), "height": video.get("height"), "sha256": sha.hexdigest(),
        "rotation": next((x.get("rotation", 0) for x in video.get("side_data_list", []) if "rotation" in x), 0)}


if __name__ == "__main__":
    try:
        print(json.dumps({"media": inspect(Path(sys.argv[2]))}), flush=True)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        print(json.dumps({"error_code": "CLIP_MEDIA_INVALID"}), flush=True)
        raise SystemExit(1)
