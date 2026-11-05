#!/usr/bin/python3
"""One-time root setup of the isolated E campaign. Never switches production.

The administrator verifies this file's hash before executing it. Public source
runs only as videohub. Credentials stay in service-account-private state.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import subprocess
import tarfile

BASE = Path("/opt/videohub/acceptance")
UNIT = "videohub-e-acceptance.service"
PRODUCTION_PYTHON = "/opt/videohub/current/backend/.venv/bin/python"
DENO_SHA256 = "368bca08999e02c6911b8f248167b582276b1f547db5712b6e736d7eda1dbda7"
REQUIRED = {"backend/scripts/acceptance_e.py", "backend/scripts/acceptance_e_trace/sitecustomize.py",
            "backend/acceptance-e-samples.json", "backend/app/main.py", "backend/uv.lock", "backend/pyproject.toml"}
DOUYIN_MANIFEST = "backend/acceptance-douyin-browser-samples.json"


def copy_runtime(origin, destination):
    # Materialize valid library links. Optional documentation links in the
    # downloaded OS packages can refer to packages outside this isolated tree.
    destination.mkdir()
    for path in origin.rglob("*"):
        if path.is_file():
            target = destination / path.relative_to(origin)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            shutil.copymode(path, target)


def runtime_digest(paths):
    records = {}
    for label, root in paths.items():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Browser runtime link escapes its directory")
                records[label + "/" + str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def verify_bundle(blob, digest):
    if len(blob) > 8 * 1024**2 or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(blob).hexdigest() != digest:
        raise ValueError("Public source bundle hash mismatch")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for entry in archive:
            path = PurePosixPath(entry.name)
            allowed = (entry.name.startswith("backend/app/") and entry.name.endswith(".py")) or entry.name in REQUIRED or entry.name in {"backend/README.md", DOUYIN_MANIFEST}
            if (not allowed or not entry.isfile() or path.is_absolute() or ".." in path.parts
                    or str(path) != entry.name or entry.name in files or any(p.startswith(".") for p in path.parts)
                    or len(files) >= 512):
                raise ValueError("Invalid public archive entry")
            total += entry.size
            if entry.size < 0 or total > 16 * 1024**2:
                raise ValueError("Public archive exceeds size limit")
            files[entry.name] = archive.extractfile(entry).read()
    if not REQUIRED <= files.keys():
        raise ValueError("Incomplete public source bundle")
    manifest = json.loads(files["backend/acceptance-e-samples.json"])
    if manifest.get("test_membership_authorized") is not True or manifest.get("duration_hours") != 72:
        raise ValueError("Campaign authorization or observation window missing")
    return files


def run(command, **kwargs):
    return subprocess.run(command, timeout=kwargs.pop("timeout", 30), check=True, **kwargs)


def install(bundle, digest, *, quick=False, browser_runtime_sha256=None):
    if os.geteuid() != 0:
        raise PermissionError("Administrator setup required")
    files = verify_bundle(bundle.read_bytes(), digest)
    browser = browser_runtime_sha256 is not None
    if browser and (not re.fullmatch(r"[0-9a-f]{64}", browser_runtime_sha256) or DOUYIN_MANIFEST not in files):
        raise ValueError("Browser runtime checksum and corpus required")
    target = BASE / (("d-" if browser else "q-" if quick else "e-") + digest[:16])
    if target.exists():
        raise ValueError("Campaign already prepared. Use its existing unit to resume")
    unit = f"videohub-douyin-{digest[:16]}.service" if browser else f"videohub-quick-{digest[:16]}.service" if quick else UNIT
    unit_path = Path("/etc/systemd/system") / unit
    if unit_path.exists():
        raise ValueError("Another campaign unit already exists")
    user, deploy = pwd.getpwnam("videohub"), pwd.getpwnam("videohub-deploy")
    if BASE.is_symlink() or BASE.stat().st_uid != 0:
        raise ValueError("Acceptance parent must be root owned")
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    public, state, reports = target / "public", target / "state", target / "reports"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    for name, content in files.items():
        path = public / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)
    for path in public.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    state.mkdir(mode=0o700)
    reports.mkdir(mode=0o711)
    state.chmod(0o700)
    reports.chmod(0o711)
    os.chown(state, user.pw_uid, user.pw_gid)
    os.chown(reports, user.pw_uid, user.pw_gid)
    deno = (Path(deploy.pw_dir) / ".local/lib/videohub-youtube/deno").read_bytes()
    if hashlib.sha256(deno).hexdigest() != DENO_SHA256:
        raise ValueError("Deno binary checksum mismatch")
    binary = state / "deno"
    binary.write_bytes(deno)
    binary.chmod(0o700)
    os.chown(binary, user.pw_uid, user.pw_gid)
    proxy = json.loads((Path(deploy.pw_dir) / ".config/videohub-youtube/client.json").read_text())
    # Capture private configuration in memory, never return it in setup output.
    loader = """
import json
from dotenv import dotenv_values
production = dotenv_values('/etc/videohub/backend.env')
values = {key: production[key] for key in ['DATABASE_URL', 'REDIS_URL']}
values['PRODUCTION_REDIS_URLS'] = [
    production.get('REDIS_URL') or 'redis://localhost:6379/0',
    production.get('CELERY_BROKER_URL') or 'redis://localhost:6379/1',
    production.get('CELERY_RESULT_BACKEND') or 'redis://localhost:6379/2',
]
print(json.dumps(values))
"""
    inputs = json.loads(run(["runuser", "-u", "videohub", "--", PRODUCTION_PYTHON, "-c", loader],
                            capture_output=True, text=True).stdout)
    inputs["YOUTUBE_PROXY"] = proxy["YOUTUBE_PROXY"]
    program = public / "backend/scripts/acceptance_e.py"
    preparation = subprocess.run(["runuser", "-u", "videohub", "--", PRODUCTION_PYTHON,
        str(program), "prepare", "--state", str(state)], input=json.dumps(inputs), capture_output=True, text=True, timeout=90)
    private_log = state / "prepare.log"
    private_log.write_text(preparation.stdout + preparation.stderr)
    private_log.chmod(0o600)
    os.chown(private_log, user.pw_uid, user.pw_gid)
    if preparation.returncode:
        raise ValueError("Isolated campaign preparation failed. Inspect private prepare.log")
    config_path = state / "private.json"
    config = json.loads(config_path.read_text())
    config["VIDEOHUB_E_REPORT"] = str(reports / "current.json")
    if browser:
        source = Path(deploy.pw_dir) / "douyin-browser-probe"
        paths = {"chromium": source / "browsers/chromium-1248", "libs": source / "libs"}
        if runtime_digest(paths) != browser_runtime_sha256:
            raise ValueError("Browser runtime checksum mismatch")
        runtime = Path("/opt/videohub/runtime/douyin") / browser_runtime_sha256[:16]
        runtime.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if runtime.parent.is_symlink() or runtime.parent.stat().st_uid != 0:
            raise ValueError("Browser runtime parent must be root owned")
        runtime.parent.chmod(0o755)
        if not runtime.exists():
            runtime.mkdir(mode=0o755)
            runtime.chmod(0o755)
            for label, origin in paths.items():
                copy_runtime(origin, runtime / label)
            for path in runtime.rglob("*"):
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        if runtime.is_symlink() or runtime.stat().st_uid != 0 or runtime_digest({label: runtime / label for label in paths}) != browser_runtime_sha256:
            raise ValueError("Installed browser runtime mismatch")
        run(["/usr/sbin/runuser", "-u", "videohub", "--", str(runtime / "chromium/chrome-linux64/chrome"), "--version"],
            capture_output=True, cwd=state,
            env={"PATH": os.defpath, "LD_LIBRARY_PATH": str(runtime / "libs/usr/lib/x86_64-linux-gnu")})
        run(["runuser", "-u", "videohub", "--", "/usr/local/bin/uv", "pip", "install", "--python",
            str(state / ".venv/bin/python"), "--index-url", "https://pypi.org/simple", "playwright==1.64.1"],
            timeout=180, capture_output=True, cwd=public / "backend",
            env={**os.environ, "UV_HTTP_TIMEOUT": "30", "UV_HTTP_RETRIES": "1"})
        config.update(DOUYIN_SOURCE_BACKEND="browser", DOUYIN_BROWSER_EXECUTABLE_PATH=str(runtime / "chromium/chrome-linux64/chrome"),
                      DOUYIN_BROWSER_LIBRARY_PATH=str(runtime / "libs/usr/lib/x86_64-linux-gnu"))
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    # The owner and mode created by the unprivileged preparation remain in place.
    python = state / ".venv/bin/python"
    if browser:
        check = """
import json, os, sys
os.environ.update(json.load(sys.stdin))
from app.modules.video.parsers.ytdlp_worker import extract_metadata
value = extract_metadata('https://www.douyin.com/video/7700513046734006379', None)
print(json.dumps({'uid': os.getuid(), 'code': value.get('error', 'OK')}))
"""
        checked = run(["runuser", "-u", "videohub", "--", "/usr/bin/setpriv", "--no-new-privs",
            str(python), "-c", check], input=json.dumps(config), text=True, capture_output=True,
            timeout=40, cwd=public / "backend")
        result = json.loads(checked.stdout)
        preflight_report = reports / "preflight.json"
        preflight_report.write_text(json.dumps(result))
        preflight_report.chmod(0o644)
        if result["code"] != "OK":
            raise ValueError("Formal-account browser preflight failed")
    unit_path.write_text(f"""[Unit]
Description=VideoHub isolated media acceptance
After=network-online.target docker.service
Wants=network-online.target
StartLimitIntervalSec=3600
StartLimitBurst=3

[Service]
User=videohub
Group=videohub
UMask=0077
WorkingDirectory={public}/backend
ExecStart={python} {program} {"run-quick" if quick or browser else "run"} --state {state}{" --manifest " + str(public / DOUYIN_MANIFEST) if browser else ""}
Restart=on-failure
RestartSec=60
RuntimeMaxSec={14400 if quick or browser else 345600}
TimeoutStopSec=60
KillMode=control-group
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={state} {reports}
PrivateTmp=true
MemoryMax=2G
CPUQuota=150%
TasksMax={512 if browser else 128}
Nice=10

[Install]
WantedBy=multi-user.target
""")
    unit_path.chmod(0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", unit])
    print(json.dumps({"installed": True, "unit": unit, "bundle_sha256": digest,
                      "report": str(reports / "current.json"), "production_application_changed": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--quick", action="store_true", help="Use a separate bounded trial regression campaign")
    parser.add_argument("--browser-runtime-sha256", help="Verify and test the preinstalled anonymous Douyin browser")
    args = parser.parse_args()
    install(args.bundle, args.sha256, quick=args.quick, browser_runtime_sha256=args.browser_runtime_sha256)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"installed": False, "error_type": type(error).__name__, "production_application_changed": False}))
        raise SystemExit(1)
