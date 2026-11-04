"""Opt-in, content-free telemetry for isolated E acceptance interpreters only."""
import functools
import json
import os
from pathlib import Path
import time
import urllib.request

trace_directory = os.environ.get("VIDEOHUB_E_TRACE_DIRECTORY")
if trace_directory:
    import fcntl
    from app.modules.download import presets

    directory = Path(trace_directory)

    def record(kind, **fields):
        # Failure of acceptance instrumentation must fail the test, not weaken
        # the network guard or silently invent fresh-network measurements.
        context = json.loads((directory / "current.json").read_text())
        event = {"operation": context["operation"], "kind": kind, "time": time.time(), **fields}
        descriptor = os.open(directory / "events.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if os.fstat(stream.fileno()).st_size > 16 * 1024**2:
                raise RuntimeError("Acceptance telemetry limit exceeded")
            stream.write(json.dumps(event) + "\n")
            stream.flush()

    original_request = urllib.request.OpenerDirector.open

    @functools.wraps(original_request)
    def open_request(self, *args, **kwargs):
        # The shared urllib transport also covers the worker run with -m.
        # Importing ytdlp_worker here would create an unpatched __main__ class.
        record("http_attempt")
        return original_request(self, *args, **kwargs)

    urllib.request.OpenerDirector.open = open_request
    original_select = presets.select_streams

    @functools.wraps(original_select)
    def select_streams(video, preset_id):
        streams = original_select(video, preset_id)
        audio_only = all(item.get("vcodec") == "none" for item in streams)
        record("selection", source_mode="independent_audio" if audio_only else "video", preset=preset_id)
        return streams

    presets.select_streams = select_streams
