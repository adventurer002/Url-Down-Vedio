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
import subprocess
import tarfile

BASE = Path("/opt/videohub/acceptance")
UNIT = "videohub-e-acceptance.service"
PRODUCTION_PYTHON = "/opt/videohub/current/backend/.venv/bin/python"
DENO_SHA256 = "368bca08999e02c6911b8f248167b582276b1f547db5712b6e736d7eda1dbda7"
REQUIRED = {"backend/scripts/acceptance_e.py", "backend/scripts/acceptance_e_trace/sitecustomize.py",
            "backend/acceptance-e-samples.json", "backend/app/main.py", "backend/uv.lock", "backend/pyproject.toml"}


def verify_bundle(blob, digest):
    if len(blob) > 8 * 1024**2 or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(blob).hexdigest() != digest:
        raise ValueError("Public source bundle hash mismatch")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for entry in archive:
            path = PurePosixPath(entry.name)
            allowed = (entry.name.startswith("backend/app/") and entry.name.endswith(".py")) or entry.name in REQUIRED or entry.name == "backend/README.md"
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


def install(bundle, digest, *, quick=False):
    if os.geteuid() != 0:
        raise PermissionError("Administrator setup required")
    files = verify_bundle(bundle.read_bytes(), digest)
    target = BASE / (("q-" if quick else "e-") + digest[:16])
    if target.exists():
        raise ValueError("Campaign already prepared. Use its existing unit to resume")
    unit = f"videohub-quick-{digest[:16]}.service" if quick else UNIT
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
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    # The owner and mode created by the unprivileged preparation remain in place.
    python = state / ".venv/bin/python"
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
ExecStart={python} {program} {"run-quick" if quick else "run"} --state {state}
Restart=on-failure
RestartSec=60
RuntimeMaxSec={14400 if quick else 345600}
TimeoutStopSec=60
KillMode=control-group
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={state} {reports}
PrivateTmp=true
MemoryMax=2G
CPUQuota=150%
TasksMax=128
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
    args = parser.parse_args()
    install(args.bundle, args.sha256, quick=args.quick)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"installed": False, "error_type": type(error).__name__, "production_application_changed": False}))
        raise SystemExit(1)
