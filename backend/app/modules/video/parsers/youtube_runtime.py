"""Offline Deno/EJS capability adapter, executed under the existing watchdog.

The CLI child never contacts YouTube, a proxy, npm or a script download service.
"""
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Protocol

from app.core.exceptions import AppException

DENO_FLAGS = ["run", "--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "--no-npm", "--cached-only",
              "--no-config", "--no-lock", "--node-modules-dir=none", "-"]


def unavailable():
    return AppException("YTDLP_RUNTIME_UNAVAILABLE", "服务器 Deno / EJS 离线能力检查未通过，请联系管理员", 503)


def packaged_ejs():
    """Check the packaged scripts against this locked SDK's version/hash table."""
    try:
        import yt_dlp_ejs
        from yt_dlp.extractor.youtube.jsc._builtin.ejs import EJSBaseJCP, ScriptType, ScriptVariant

        if str(yt_dlp_ejs.version).split(".")[:2] != str(EJSBaseJCP._SCRIPT_VERSION).split(".")[:2]:
            raise ValueError
        scripts = []
        for kind, load in ((ScriptType.LIB, yt_dlp_ejs.yt.solver.lib), (ScriptType.CORE, yt_dlp_ejs.yt.solver.core)):
            code = load()
            expected = EJSBaseJCP._ALLOWED_HASHES[kind][ScriptVariant.MINIFIED]
            if not isinstance(code, str) or hashlib.sha3_512(code.encode()).hexdigest() != expected:
                raise ValueError
            scripts.append(code)
        return version("yt-dlp-ejs"), scripts
    except Exception:
        raise unavailable() from None


def smoke_script(scripts, marker):
    # Match the official provider's global initialization, without a network player.
    return "\n".join([scripts[0], "Object.assign(globalThis, lib);", scripts[1],
                      "if (typeof jsc !== 'function') throw new Error('EJS unavailable');",
                      f"console.log('{marker}')"])


class YoutubeRuntimeCapabilities(Protocol):
    async def check(self) -> dict: ...


class DenoEjsCapabilities:
    async def check(self):
        from app.infrastructure.process_runner import ProcessRunner

        async def never_cancel(): return False
        async def no_progress(event): pass

        with tempfile.TemporaryDirectory(prefix="videohub-youtube-runtime-") as directory:
            runner = ProcessRunner(timeout=8, max_bytes=1024 * 1024)
            try:
                return await runner.run([sys.executable, "-m", __name__, directory], Path(directory),
                    cwd=Path(__file__).resolve().parents[4], is_cancel_requested=never_cancel, on_progress=no_progress)
            except AppException as error:
                if error.code == "PROCESS_CLEANUP_FAILED":
                    raise
                raise unavailable() from None


def inspect_runtime(directory):
    from app.modules.video.parsers.runtime import javascript_runtime_options

    options = javascript_runtime_options("https://www.youtube.com/")
    executable = options.get("deno", {}).get("path")
    if not executable:
        raise unavailable()
    ejs_version, scripts = packaged_ejs()
    # Deno inherits the watched child's process group. The parent watchdog also
    # stops descendants on cancellation/timeout, independent of Solo signals.
    environment = {**os.environ, "DENO_DIR": str(Path(directory) / "deno")}
    result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2, env=environment)
    match = re.search(r"^deno (\d+)\.(\d+)\.(\d+)", result.stdout)
    if result.returncode or not match or tuple(map(int, match.groups())) < (2, 3, 0):
        raise unavailable()
    marker = "VIDEOHUB_OFFLINE_EJS_OK"
    script = smoke_script(scripts, marker)
    result = subprocess.run([executable, *DENO_FLAGS], input=script, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, timeout=3, env=environment)
    if result.returncode or result.stdout.strip() != marker:
        raise unavailable()
    return {"deno_version": ".".join(match.groups()), "ejs_version": ejs_version,
            "ejs_verified": True, "sandbox_verified": True}


def main():
    try:
        result = inspect_runtime(sys.argv[1])
    except Exception:
        result = {"error_code": "YTDLP_RUNTIME_UNAVAILABLE"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
