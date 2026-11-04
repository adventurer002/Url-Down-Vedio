"""Run isolated server preflight and an optional YouTube metadata operation.

Uses exact locked packages from the server uv cache and a private proxy file.
The report contains sanitized capabilities and source hashes. Media tasks use
normal TaskService entry points separately.
"""
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

PROBE_PROGRAM = r'''
import asyncio
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from app.core.exceptions import AppException
from app.core.security import validate_source_url
from app.modules.video.parsers.links import canonical_link
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.preflight import check
from app.modules.video.service import aggregate_download_options

url, client_path = sys.argv[1:]
private_directory = Path(client_path).parent
cooldown_path = private_directory / "metadata-probe-cooldown.json"
source_paths = [
    "app/modules/video/parsers/youtube.py",
    "app/modules/video/parsers/youtube_dns.py",
    "app/modules/video/parsers/youtube_runtime.py",
    "app/modules/video/parsers/ytdlp_worker.py",
    "app/modules/video/parsers/ytdlp.py",
    "app/core/security.py",
]
result = {
    "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "mode": "isolated_server_preflight",
    "account": "videohub-deploy",
    "service_account_checked": False,
    "production_application_changed": False,
    "download_verified": False,
    "standalone_verified": False,
    "youtube_network_probe_performed": False,
    "metadata_verified": False,
    "code_hashes": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in source_paths},
}


def remember_cooldown():
    with tempfile.NamedTemporaryFile(mode="w", dir=private_directory, delete=False) as output:
        json.dump({"until": time.time() + 300}, output)
        pending = Path(output.name)
    pending.chmod(0o600)
    os.replace(pending, cooldown_path)


async def main():
    result["configuration"] = await check(platform="youtube")
    if url == "-" or not result["configuration"]["passed"]:
        return
    lock_path = private_directory / "metadata-probe.lock"
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["code"] = "PARSE_BUSY"
            return
        if cooldown_path.exists():
            state = json.loads(cooldown_path.read_text())
            if state.get("until", 0) > time.time():
                result["code"] = "SOURCE_COOLDOWN"
                return
        started = time.monotonic()
        try:
            normalized = await validate_source_url(canonical_link(url))
            result["youtube_network_probe_performed"] = True
            video = await YtDlpParser()._parse(normalized, 30)
            result.update(
                code="OK",
                metadata_verified=True,
                video_id=video.platform_video_id,
                duration=video.duration,
                presets=[preset.id for preset in aggregate_download_options(video)],
                subtitle_languages=video.subtitle_languages,
            )
        except AppException as error:
            failure = getattr(error, "source_failure", None)
            result.update(code=error.code, source_failure=failure)
            if failure in {"restricted", "rate_limited"}:
                remember_cooldown()
        result["metadata_seconds"] = round(time.monotonic() - started, 3)


asyncio.run(main())
print(json.dumps(result, ensure_ascii=False))
'''

REMOTE_BOOTSTRAP = r'''
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv

binary, cache, client_path, deno_path, url = sys.argv[1:]
with tempfile.TemporaryDirectory(prefix="videohub-youtube-c-probe-") as directory:
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:gz") as archive:
        archive.extractall(directory, filter="data")
    root = Path(directory) / "backend"
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    locked = {normalize(package["name"]): package["version"] for package in tomllib.loads((root / "uv.lock").read_text())["package"]}
    archives = set()
    for metadata in Path(cache).glob("archive-v0/*/*.dist-info/METADATA"):
        content = metadata.read_text()
        name = re.search(r"^Name: (.+)$", content, re.M)
        version = re.search(r"^Version: (.+)$", content, re.M)
        if name and version and locked.get(normalize(name[1])) == version[1]:
            archives.add(str(metadata.parent.parent))
    if not archives:
        raise SystemExit("Locked server dependency cache unavailable")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root / ".venv")
    site_packages = next((root / ".venv/lib").glob("python*/site-packages"))
    (site_packages / "locked-cache.pth").write_text("\n".join(sorted(archives)) + "\n")
    private = json.loads(Path(client_path).read_text())
    environment = {
        **os.environ,
        "VIDEO_EGRESS_MODE": "platform",
        "YTDLP_PROXY": "",
        "YOUTUBE_PROXY": private["YOUTUBE_PROXY"],
        "YOUTUBE_DNS_MODE": private.get("YOUTUBE_DNS_MODE", "system"),
        "YTDLP_JS_RUNTIME": "deno",
        "YTDLP_DENO_PATH": deno_path,
    }
    process = subprocess.Popen(
        [str(root / ".venv/bin/python"), "-c", PROBE_PROGRAM, url, client_path],
        cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=65)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate(timeout=2)
    if process.returncode:
        raise SystemExit("Isolated preflight failed. Private diagnostic output was suppressed")
    print(json.dumps(json.loads(stdout), ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--client-config", required=True, help="Private server JSON containing YOUTUBE_PROXY")
    parser.add_argument("--deno", required=True)
    parser.add_argument("--url", help="Optional public video metadata operation")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    if args.url:
        from app.modules.video.parsers.links import canonical_link, platform_for
        if platform_for(canonical_link(args.url)) != "youtube":
            parser.error("A YouTube video is required")
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        paths = sorted((root / "app").rglob("*.py"))
        paths.extend(root / name for name in ("pyproject.toml", "uv.lock", "README.md"))
        for path in paths:
            archive.add(path, arcname=str(Path("backend") / path.relative_to(root)), recursive=False)
    remote_program = "PROBE_PROGRAM = " + repr(PROBE_PROGRAM) + "\n" + REMOTE_BOOTSTRAP
    remote_command = shlex.join([
        args.python, "-c", remote_program, args.python, args.cache,
        args.client_config, args.deno, args.url or "-",
    ])
    command = [
        "ssh", "-i", str(args.identity), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", "--", args.host, remote_command,
    ]
    response = subprocess.run(command, input=bundle.getvalue(), capture_output=True, timeout=90)
    if response.returncode:
        raise SystemExit("Server preflight unavailable. Private logs were suppressed")
    result = json.loads(response.stdout)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
