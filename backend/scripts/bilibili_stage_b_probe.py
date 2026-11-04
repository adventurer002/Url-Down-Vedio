"""One isolated Bilibili or Douyin metadata probe over SSH.

Run from backend/. The selected Python must already exist on the server. The
temporary venv reads exact locked versions from a public server uv cache and is
deleted after the probe. No package download or production venv access occurs.
This does not run media tasks and never claims complete download verification.
"""
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

REMOTE = r'''
import asyncio, datetime, io, json, os, pathlib, re, signal, subprocess, sys, tarfile, tempfile, time, tomllib, venv
binary, url, cache, platform = sys.argv[1:]
with tempfile.TemporaryDirectory(prefix="videohub-bili-b-") as directory:
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:gz") as archive:
        archive.extractall(directory, filter="data")
    root = directory + "/backend"
    environment = {**os.environ, "VIDEO_EGRESS_MODE": "platform",
        "YTDLP_PROXY": "", "YOUTUBE_PROXY": "", "YTDLP_JS_RUNTIME": "disabled"}
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    locked = {normalize(p["name"]):p["version"] for p in tomllib.loads(pathlib.Path(root + "/uv.lock").read_text())["package"]}
    archives = set()
    for metadata in pathlib.Path(cache).glob("archive-v0/*/*.dist-info/METADATA"):
        content = metadata.read_text()
        name = re.search(r"^Name: (.+)$", content, re.M)
        version = re.search(r"^Version: (.+)$", content, re.M)
        if name and version and locked.get(normalize(name[1])) == version[1]:
            archives.add(str(metadata.parent.parent))
    if not archives:
        print(json.dumps({"code":"PROBE_ENVIRONMENT_UNAVAILABLE","production_changed":False,"download_verified":False}))
        sys.exit(0)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root + "/.venv")
    site = next(pathlib.Path(root + "/.venv/lib").glob("python*/site-packages"))
    (site / "locked-cache.pth").write_text("\n".join(sorted(archives)) + "\n")
    program = r"""
import asyncio, datetime, hashlib, json, math, pathlib, sys, time
from contextlib import redirect_stdout
from app.core.exceptions import AppException
from app.modules.video.parsers.bilibili import BilibiliClient
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.parsers.links import canonical_link, resolve_short_link
from app.modules.video.parsers import douyin
from app.modules.video.parsers.ytdlp_worker import extract_metadata
from app.modules.video.service import aggregate_download_options
observations=[]
original=BilibiliClient.json
def recorded(self,*args,**kwargs):
    payload=original(self,*args,**kwargs)
    item={"stage":self.stage}
    code=payload.get("code")
    if isinstance(code,int) and not isinstance(code,bool): item["api_code"]=code
    if self.stage=="play":
        data=payload.get("data")
        if isinstance(data,dict):
            length=data.get("timelength")
            if isinstance(length,(int,float)) and math.isfinite(length): item["timelength_ms"]=length
            item["is_preview"]=bool(data.get("is_preview") or data.get("is_trial"))
        if self.expected: item["expected_duration_seconds"]=self.expected["duration"]
    observations.append(item)
    return payload
BilibiliClient.json=recorded
original_share_page = douyin.read_share_page
def recorded_share_page(downloader, identifier):
    page = original_share_page(downloader, identifier)
    observations.append({"stage":"share_page", "bytes":len(page.encode()),
        "router_data_present":"_ROUTER_DATA" in page, "render_data_present":"RENDER_DATA" in page})
    return page
douyin.read_share_page=recorded_share_page
started=time.monotonic()
result={"checked_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "mode":"isolated_metadata_probe", "egress":"direct", "production_changed":False,
    "download_verified":False, "sample_url":sys.argv[1], "dependency_source":"locked_public_uv_cache",
    "adapter_sha256":hashlib.sha256(pathlib.Path("app/modules/video/parsers/" + sys.argv[2] + ".py").read_bytes()).hexdigest(),
    "code_hashes": {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(pathlib.Path("app").rglob("*.py"))}}
try:
    with redirect_stdout(sys.stderr):
        normalized=asyncio.run(resolve_short_link(canonical_link(sys.argv[1])))
        extracted=extract_metadata(normalized,None)
    if "error" in extracted:
        error=AppException(extracted["error"],"Source probe failed")
        error.source_failure=extracted.get("source_failure")
        error.source_stage=extracted.get("source_stage")
        raise error
    video=ParsedVideo(**extracted["data"])
    result.update(code="OK",platform=video.platform,video_id=video.platform_video_id,duration=video.duration,
        formats=len(video.formats),subtitle_languages=video.subtitle_languages,
        presets=[item.id for item in aggregate_download_options(video)])
except AppException as error:
    result.update(code=error.code,source_failure=getattr(error,"source_failure",None),source_stage=getattr(error,"source_stage",None))
result["elapsed_seconds"]=round(time.monotonic()-started,3)
result["api_observations"]=observations
print(json.dumps(result,ensure_ascii=False))
"""
    process = subprocess.Popen([root + "/.venv/bin/python", "-c", program, url, platform], cwd=root,
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=38)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate(timeout=2)
    if process.returncode:
        print(json.dumps({"code":"PROBE_FAILED", "production_changed":False, "download_verified":False}))
    else:
        print(json.dumps(json.loads(stdout),ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH user@host, using known_hosts verification")
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--python", required=True, help="Existing accessible server Python >= 3.14")
    parser.add_argument("--cache", required=True, help="Readable server uv cache; exact uv.lock versions only")
    parser.add_argument("--platform", choices=["bilibili", "douyin"], default="bilibili")
    parser.add_argument("--url", help="Public video link or share text")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from app.modules.video.parsers.links import canonical_link, is_domestic_short_link, platform_for
    args.url = args.url or {"bilibili": "https://www.bilibili.com/video/BV1g2JX6nESP?t=3.6",
                           "douyin": "https://www.douyin.com/video/7700513046734006379"}[args.platform]
    args.url = canonical_link(args.url)
    if platform_for(args.url) != args.platform:
        parser.error("The video platform must match --platform")
    if not is_domestic_short_link(args.url):
        if args.platform == "bilibili":
            from app.modules.video.parsers.bilibili import video_reference
        else:
            from app.modules.video.parsers.douyin import video_reference
        video_reference(args.url)
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        paths = sorted((root / "app").rglob("*.py")) + [root / name for name in ("pyproject.toml", "uv.lock", "README.md")]
        for path in paths:
            archive.add(path, arcname=str(Path("backend") / path.relative_to(root)), recursive=False)
    command = ["ssh", "-i", str(args.identity), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", "--", args.host,
        shlex.join([args.python, "-c", REMOTE, args.python, args.url, args.cache, args.platform])]
    result = subprocess.run(command, input=bundle.getvalue(), capture_output=True, timeout=145)
    if result.returncode:
        raise SystemExit("SSH probe failed; production was not changed")
    report = json.loads(result.stdout)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
