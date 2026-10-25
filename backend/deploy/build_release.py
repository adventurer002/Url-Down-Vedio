"""Package committed backend source and the frontend built by this CI run."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def build(commit, frontend_dist, output_dir):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit must be a full Git SHA")
    if not (frontend_dist / "index.html").is_file():
        raise ValueError("frontend dist/index.html is required")
    selected = ["backend/app", "backend/alembic", "backend/alembic.ini",
                "backend/pyproject.toml", "backend/uv.lock", "backend/README.md", "backend/deploy"]
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", commit, "--", *selected], cwd=ROOT)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Only regular files may be packaged: {member.name}")
            if PurePosixPath(member.name).name == ".gitkeep":
                continue
            path = PurePosixPath(member.name)
            if any(part.startswith(".") for part in path.parts) or path.name.endswith(".env"):
                raise ValueError(f"Unexpected hidden/config file: {member.name}")
            files[member.name] = source.extractfile(member).read()
    for required in ("backend/app/main.py", "backend/alembic.ini", "backend/pyproject.toml", "backend/uv.lock"):
        if required not in files:
            raise ValueError(f"Missing required backend file: {required}")
    for path in sorted(frontend_dist.rglob("*")):
        if path.is_symlink():
            raise ValueError("Frontend symlinks are not supported")
        if not path.is_file():
            continue
        relative = path.relative_to(frontend_dist)
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("Unexpected hidden frontend file")
        files[f"frontend/dist/{relative.as_posix()}"] = path.read_bytes()
    qr = files.get("frontend/dist/wechat_qr.png")
    if qr is not None:
        files["frontend/public/wechat_qr.png"] = qr
    manifest = {
        "schema_version": 1, "commit": commit, "api_base_url": "/api/v1",
        "backend_dependencies": "Install on Linux with uv sync --frozen --no-dev",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    files["release.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"videohub-{commit}.tar.gz"
    with tarfile.open(target, "w:gz") as output:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            output.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(f"Release: {target.name}; {len(files)} files; SHA256 {digest}")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--frontend-dist", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    build(args.commit, args.frontend_dist.resolve(), args.output_dir.resolve())
