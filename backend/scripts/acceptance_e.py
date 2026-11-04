"""Bounded multiday acceptance against an isolated, authenticated API and worker.

Installed public code is immutable. Private state, quotas, schema and queue are
campaign-scoped. No production release, model call or upstream cookie is used.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from uuid import UUID, uuid4

BACKEND = Path(__file__).resolve().parents[1]
PLATFORMS = ("bilibili", "youtube", "douyin")
CAPABILITIES = ("metadata", "audio", "video")
TERMINAL = {"completed", "failed", "cancelled", "expired"}
REQUIRED_SAFETY = ("service_account", "owner_isolation", "completed_state", "cancellation", "telemetry", "host_reboot", "ci_release_available")
QUICK_VIDEO_IDS = {"BV1RA4w6AE74", "BV1XJ4M61E1J", "BV1bn4P6bEpL", "18NGQq7p3LY", "FIl7x6_3R5Y", "ScMzIvxBSi4"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value, mode=0o600):
    pending = path.with_name(path.name + "." + uuid4().hex + ".pending")
    descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def validate_manifest(manifest):
    sys.path.insert(0, str(BACKEND))
    from app.modules.video.parsers.links import canonical_link, platform_for
    from app.modules.video.parsers.bilibili import video_reference as bili_reference
    from app.modules.video.parsers.douyin import video_reference as douyin_reference
    from urllib.parse import parse_qs, urlsplit
    if manifest.get("schema_version") != 1 or manifest.get("duration_hours") != 72:
        raise ValueError("Invalid acceptance manifest version or observation window")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not 1 <= len(samples) <= 100:
        raise ValueError("Invalid sample count")
    seen, identities = set(), set()
    for sample in samples:
        identifier, platform = sample["sample_id"], sample["platform"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or identifier in seen or platform not in PLATFORMS:
            raise ValueError("Invalid or duplicate sample")
        normalized = canonical_link(sample["url"])
        if normalized != sample["url"] or platform_for(normalized) != platform:
            raise ValueError("Manifest URLs must be canonical platform links")
        if platform == "bilibili":
            bili_reference(normalized)
        elif platform == "douyin":
            douyin_reference(normalized)
        else:
            parts = urlsplit(normalized)
            if parts.hostname != "www.youtube.com" or parts.path != "/watch" or parse_qs(parts.query).get("v") != [sample["video_id"]]:
                raise ValueError("Invalid YouTube identity")
        identity = (platform, sample["video_id"])
        if identity in identities or sample["scheduled_day"] not in (0, 1, 2):
            raise ValueError("Duplicate source identity or invalid schedule")
        if sample["expected"] != "public_video":
            raise ValueError("Negative cases require a separate corpus")
        seen.add(identifier)
        identities.add(identity)
    return manifest


def summarize(manifest, observations, *, elapsed_hours, safety):
    """One final observation per preregistered logical request. No denominator pruning."""
    expected = {(s["sample_id"], cap): s for s in manifest["samples"] for cap in CAPABILITIES}
    indexed = {}
    for observation in observations:
        key = (observation["sample_id"], observation["capability"])
        if key not in expected or key in indexed:
            raise ValueError("Unknown or duplicate logical result")
        indexed[key] = observation
    safety = dict(safety)
    failure_checks = {"OWNER_CHECK_FAILED": "owner_isolation", "COMPLETED_STATE_CHANGED": "completed_state",
                      "TELEMETRY_MISSING": "telemetry"}
    for observation in observations:
        failed = failure_checks.get(observation["code"])
        if failed:
            safety[failed] = False
    output = {}
    for platform in PLATFORMS:
        samples = [s for s in manifest["samples"] if s["platform"] == platform]
        result = {"registered_samples": len(samples), "capabilities": {}}
        eligible = len(samples) >= 30 and elapsed_hours >= 72 and all(safety.get(name) is True for name in REQUIRED_SAFETY)
        for capability in CAPABILITIES:
            rows = [indexed[(s["sample_id"], capability)] for s in samples if (s["sample_id"], capability) in indexed]
            success = sum(row["code"] == "OK" for row in rows)
            fresh = [row for row in rows if row.get("http_attempts", 0) > 0]
            days = sorted({row["finished_at"][:10] for row in rows})
            errors = {}
            for row in rows:
                if row["code"] != "OK":
                    errors[row["code"]] = errors.get(row["code"], 0) + 1
            threshold = 29 if capability == "metadata" else 27
            passed = len(samples) >= 30 and len(rows) == len(samples) and success * 30 >= threshold * len(samples) and len(days) >= 3
            result["capabilities"][capability] = {"success": success, "attempted_logical": len(rows),
                "registered_denominator": len(samples), "pending": len(samples) - len(rows),
                "fresh_attempted": len(fresh), "fresh_success": sum(row["code"] == "OK" for row in fresh),
                "observation_dates_utc": days, "errors": errors, "threshold_passed": passed,
                "independent_audio_success": sum(row["code"] == "OK" and row.get("source_mode") == "independent_audio" for row in rows) if capability == "audio" else None}
            eligible = eligible and passed
        result["release_eligible"] = eligible
        output[platform] = result
    return {"platforms": output, "elapsed_hours": round(elapsed_hours, 3), "safety": safety,
            "production_release_performed": False}


def process_groups(parent):
    family = {parent}
    entries = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                entries.append((int(entry.name), int(fields[1]), int(fields[2])))
            except (OSError, ValueError, IndexError):
                pass
    for _ in range(16):
        children = {pid for pid, ppid, _ in entries if ppid in family}
        if children <= family:
            break
        family |= children
    return {group for pid, _, group in entries if pid in family}


def stop_process(process):
    groups = process_groups(process.pid) | {process.pid}
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def load_environment(state):
    config = json.loads((state / "private.json").read_text())
    os.environ.update(config)
    sys.path.insert(0, str(BACKEND))
    schema = config["VIDEOHUB_E_SCHEMA"]
    if not re.fullmatch(r"videohub_e_[0-9a-f]{32}", schema):
        raise ValueError("Invalid acceptance schema")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.infrastructure.database import session as database
    engine = create_engine(config["DATABASE_URL"], pool_timeout=1, pool_pre_ping=True,
        connect_args={"connect_timeout": 2, "options": "-c search_path=" + schema + " -c statement_timeout=5000"})
    database.engine = engine
    database.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return config, engine


def production_redis_databases(urls):
    from urllib.parse import urlsplit
    excluded = {0, 1, 2}
    for value in urls:
        configured = urlsplit(value)
        if configured.scheme in {"redis", "rediss"}:
            excluded.add(int(configured.path.lstrip("/") or "0"))
    return excluded


def prepare(state):
    import tomllib
    import venv
    from urllib.parse import urlsplit, urlunsplit
    from redis import Redis
    from sqlalchemy import create_engine, text
    inputs = json.load(sys.stdin)
    locked = {re.sub(r"[-_.]+", "-", p["name"]).lower(): p["version"] for p in tomllib.loads((BACKEND / "uv.lock").read_text())["package"]}
    archives = set()
    for metadata in Path("/opt/videohub/.cache/uv").glob("archive-v0/*/*.dist-info/METADATA"):
        content = metadata.read_text()
        name = re.search(r"^Name: (.+)$", content, re.M)
        version = re.search(r"^Version: (.+)$", content, re.M)
        if name and version and locked.get(re.sub(r"[-_.]+", "-", name[1]).lower()) == version[1]:
            archives.add(str(metadata.parent.parent))
    if not archives:
        raise ValueError("Locked cache unavailable")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(state / ".venv")
    site = next((state / ".venv/lib").glob("python*/site-packages"))
    (site / "locked-cache.pth").write_text("\n".join(sorted(archives)) + "\n")
    # Dedicated initially empty DB. Never clear or reuse a populated Redis DB.
    parts = urlsplit(inputs["REDIS_URL"])
    if parts.scheme not in {"redis", "rediss"}:
        raise ValueError("Unsupported production Redis transport")
    excluded = production_redis_databases(inputs.get("PRODUCTION_REDIS_URLS", [inputs["REDIS_URL"]]))
    redis_url, database_number = None, None
    for number in range(3, 16):
        if number in excluded:
            continue
        candidate = urlunsplit((parts.scheme, parts.netloc, "/" + str(number), "", ""))
        client = Redis.from_url(candidate, socket_connect_timeout=2, socket_timeout=2)
        try:
            if client.dbsize() == 0 and client.set("videohub-e:reserved", state.name, nx=True, ex=5 * 86400):
                redis_url, database_number = candidate, number
                break
        finally:
            client.close()
    if redis_url is None:
        raise ValueError("No empty Redis database")
    identity = uuid4().hex
    config = {"DATABASE_URL": inputs["DATABASE_URL"], "REDIS_URL": redis_url,
        "CELERY_BROKER_URL": redis_url, "CELERY_RESULT_BACKEND": "cache+memory://",
        "APP_ENV": "development", "AUTH_REQUIRED": "true", "ALLOWANCES_ENABLED": "true",
        "WECHAT_MP_MODE": "mock", "WECHAT_MP_STATIC_QR_URL": "", "WECHAT_MP_TOKEN": os.urandom(32).hex(),
        "JWT_SECRET_KEY": os.urandom(48).hex(), "VIDEO_EGRESS_MODE": "platform", "YTDLP_PROXY": "",
        "YOUTUBE_PROXY": inputs["YOUTUBE_PROXY"], "YOUTUBE_DNS_MODE": "proxy_doh",
        "YTDLP_JS_RUNTIME": "deno", "YTDLP_DENO_PATH": str(state / "deno"),
        "DENO_DIR": str(state / "deno-cache"), "ASR_PROVIDER": "disabled", "LLM_PROVIDER": "heuristic", "CLIPS_ENABLED": "false",
        "STORAGE_LOCAL_PATH": str(state / "storage"), "STORAGE_MAX_QUOTA_MB": "5120", "STORAGE_FILE_TTL_HOURS": "1",
        "MAX_FILE_SIZE_MB": "512", "DOWNLOAD_TIMEOUT_SECONDS": "600", "VIDEO_PARSE_CACHE_TTL_SECONDS": "0",
        "VIDEOHUB_E_SCHEMA": "videohub_e_" + identity, "VIDEOHUB_E_QUEUE": "videohub-e-" + identity,
        "VIDEOHUB_E_REDIS_DB": str(database_number), "VIDEOHUB_E_TRACE_DIRECTORY": str(state / "trace"),
        "PYTHONPATH": str(BACKEND / "scripts/acceptance_e_trace") + os.pathsep + str(BACKEND), "PYTHONUNBUFFERED": "1"}
    (state / "trace").mkdir(mode=0o700)
    atomic_json(state / "trace/current.json", {"operation": "setup"})
    atomic_json(state / "private.json", config)
    engine = create_engine(config["DATABASE_URL"], connect_args={"connect_timeout": 2, "options": "-c statement_timeout=5000"})
    try:
        with engine.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + config["VIDEOHUB_E_SCHEMA"] + '"'))
    finally:
        engine.dispose()
    print(json.dumps({"prepared": True, "redis_db": database_number}))


def serve(state, mode):
    config, engine = load_environment(state)
    from app.infrastructure.queue.celery_app import celery_app
    celery_app.conf.task_default_queue = config["VIDEOHUB_E_QUEUE"]
    if mode == "worker":
        celery_app.worker_main(["worker", "--pool=solo", "-c", "1", "--loglevel=warning",
            "--without-gossip", "--without-mingle", "--without-heartbeat", "--hostname", config["VIDEOHUB_E_QUEUE"] + "@%h"])
    else:
        import uvicorn
        uvicorn.run("app.main:create_app", factory=True, fd=int(os.environ["VIDEOHUB_E_FD"]), log_level="warning")


class ProbeFailure(Exception):
    def __init__(self, code):
        self.code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", str(code)) else "ACCEPTANCE_FAILED"


class Campaign:
    def __init__(self, state):
        self.state = state
        self.config, self.engine = load_environment(state)
        self.manifest = validate_manifest(json.loads((BACKEND / "acceptance-e-samples.json").read_text()))
        self.path = state / "progress.json"
        self.progress = json.loads(self.path.read_text()) if self.path.exists() else {
            "started_at": timestamp(), "observations": [], "checks": {}, "pending": None,
            "membership_authorized": True, "production_application_changed": False,
            "login_mode": "isolated_development_mock", "account_uid": os.getuid()}
        self.processes, self.logs = [], []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.operation = "setup"
        self.persist()

    def persist(self):
        atomic_json(self.path, self.progress)
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(self.progress["started_at"])).total_seconds() / 3600
        safety = {name: self.progress["checks"].get(name) is True for name in REQUIRED_SAFETY}
        report = {"status": self.progress.get("status", "running"), "started_at": self.progress["started_at"],
            "acceptance_mode": self.progress.get("acceptance_mode", "multiday"),
            "trial_eligible": self.progress.get("trial_eligible", False),
            "updated_at": timestamp(), "membership_authorized": True, "login_mode": "isolated_development_mock",
            "account_uid": os.getuid(), "paid_model_calls": False, "production_application_changed": False,
            "manifest_sha256": hashlib.sha256((BACKEND / "acceptance-e-samples.json").read_bytes()).hexdigest(),
            "source_hashes": {str(p.relative_to(BACKEND)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((BACKEND / "app").rglob("*.py"))},
            "observations": self.progress["observations"], "checks": self.progress["checks"],
            "membership_audit": self.progress.get("membership_audit"),
            **summarize(self.manifest, self.progress["observations"], elapsed_hours=elapsed, safety=safety)}
        atomic_json(Path(self.config["VIDEOHUB_E_REPORT"]), report, 0o644)

    def context(self, operation):
        self.operation = operation
        atomic_json(self.state / "trace/current.json", {"operation": operation})

    def trace(self):
        path = self.state / "trace/events.jsonl"
        if not path.exists():
            return []
        return [row for line in path.read_text().splitlines() if (row := json.loads(line)).get("operation") == self.operation]

    def data(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            try:
                code = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "HTTP_" + str(response.status_code)
            raise ProbeFailure(code)
        return response.json()["data"]

    def login(self, label):
        import xml.etree.ElementTree as ET
        scene = self.data("POST", "/auth/wechat/sessions")
        token = self.config["WECHAT_MP_TOKEN"]
        nonce, instant = uuid4().hex, str(int(time.time()))
        signature = hashlib.sha1("".join(sorted([token, instant, nonce])).encode()).hexdigest()
        root = ET.Element("xml")
        for key, value in {"FromUserName": "e_" + self.config["VIDEOHUB_E_SCHEMA"] + "_" + label,
                           "ToUserName": "isolated", "MsgType": "text", "Content": scene["verify_code"],
                           "CreateTime": instant, "MsgId": nonce}.items():
            ET.SubElement(root, key).text = value
        response = self.client.post("/auth/wechat/webhook", params={"signature": signature, "timestamp": instant, "nonce": nonce},
                                    content=ET.tostring(root), headers={"Content-Type": "application/xml"})
        if response.status_code != 200:
            raise ProbeFailure("LOGIN_FAILED")
        return self.data("GET", "/auth/wechat/sessions/" + scene["scene_id"])

    def start(self):
        import pwd
        import httpx
        from app.infrastructure.database.base import Base
        from app.infrastructure.database import models
        from app.modules.admin.service import AdminService
        from app.modules.billing.manage import recover
        from app.modules.tasks.repository import TaskRepository
        self.progress["checks"]["service_account"] = os.getuid() == pwd.getpwnam("videohub").pw_uid
        if not self.progress["checks"]["service_account"]:
            raise ProbeFailure("WRONG_SERVICE_ACCOUNT")
        Base.metadata.create_all(self.engine)
        recover(TaskRepository(__import__("app.infrastructure.database.session", fromlist=["SessionLocal"]).SessionLocal), self.state / "storage")
        for mode in ("worker", "api"):
            log = (self.state / (mode + ".log")).open("a")
            self.logs.append(log)
            env = {**os.environ, "VIDEOHUB_E_FD": str(self.listener.fileno())}
            process = subprocess.Popen([sys.executable, str(Path(__file__)), mode, "--state", str(self.state)],
                cwd=BACKEND, env=env, stdout=log, stderr=log, start_new_session=True,
                pass_fds=(self.listener.fileno(),) if mode == "api" else ())
            self.processes.append(process)
        self.client = httpx.Client(base_url="http://127.0.0.1:" + str(self.listener.getsockname()[1]) + "/api/v1",
                                   trust_env=False, timeout=httpx.Timeout(40, connect=3))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in self.processes):
                raise ProbeFailure("API_WORKER_EXITED")
            try:
                if self.client.get("/health/ready", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.5)
        else:
            raise ProbeFailure("API_NOT_READY")
        owner = self.login("owner")
        self.client.headers["Authorization"] = "Bearer " + owner["access_token"]
        from app.infrastructure.database.session import SessionLocal
        if not self.progress.get("membership_audit"):
            result = AdminService(SessionLocal).grant_user(None, UUID(owner["user"]["id"]), "monthly",
                "2026-11-04 用户授权的阶段 E 隔离测试权益，非支付", "stage-e-member", "stage_e_" + uuid4().hex,
                operator="codex_user_authorized_stage_e")
            self.progress["membership_audit"] = {"audit_id": result["audit_id"], "payment_verified": False, "isolated_only": True}
        self.owner_token = owner["access_token"]
        self.other_token = self.login("other")["access_token"]
        self.client.headers["Authorization"] = "Bearer " + self.owner_token
        self.persist()

    def completed(self, task_id, timeout=620):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.data("GET", "/tasks/" + task_id)
            if task["status"] in TERMINAL:
                return task
            if any(p.poll() is not None for p in self.processes):
                raise ProbeFailure("API_WORKER_EXITED")
            time.sleep(.5)
        self.data("POST", "/tasks/" + task_id + "/cancel")
        raise ProbeFailure("TASK_TIMEOUT")

    def create(self, url, preset, pending_key="pending"):
        quote = self.data("POST", "/billing/quotes", json={"url": url, "action": "download", "preset_id": preset})
        self.progress[pending_key]["quote_id"] = quote["quote_id"]
        self.persist()
        task_id = self.data("POST", "/billing/tasks", json={"quote_id": quote["quote_id"]})["task_id"]
        self.progress[pending_key]["task_id"] = task_id
        self.persist()
        return task_id

    def verify_file(self, final, metadata, capability):
        file_id = final["result_payload"]["file_id"]
        output = self.state / ("delivery.mp3" if capability == "audio" else "delivery.mp4")
        size = 0
        try:
            deadline = time.monotonic() + 180
            with self.client.stream("GET", "/files/" + file_id + "/download", timeout=5) as response:
                if response.status_code != 200:
                    raise ProbeFailure("FILE_DELIVERY_FAILED")
                with output.open("wb") as destination:
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise ProbeFailure("FILE_DELIVERY_TIMEOUT")
                        size += len(chunk)
                        if size > 512 * 1024**2:
                            raise ProbeFailure("FILE_TOO_LARGE")
                        destination.write(chunk)
            if size != final["result_payload"]["size_bytes"] or size == 0:
                raise ProbeFailure("DELIVERY_SIZE_MISMATCH")
            media = json.loads(subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe,crypto",
                "-show_format", "-show_streams", "-of", "json", str(output)],
                check=True, capture_output=True, text=True, timeout=15, start_new_session=True).stdout)
            duration = float(media["format"]["duration"])
            if abs(duration - metadata["duration"]) > max(2, metadata["duration"] * .02):
                raise ProbeFailure("MEDIA_DURATION_MISMATCH")
            tracks = sorted({item["codec_type"] for item in media["streams"]})
            if "audio" not in tracks or (("video" in tracks) != (capability == "video")):
                raise ProbeFailure("MEDIA_TRACK_MISMATCH")
            dimensions = {}
            if capability == "video":
                stream = next(item for item in media["streams"] if item["codec_type"] == "video")
                dimensions = {name: stream[name] for name in ("width", "height")}
                from app.infrastructure.database.session import SessionLocal
                from app.modules.tasks.repository import TaskRepository
                selection = TaskRepository(SessionLocal).get(UUID(final["id"])).input_payload.get("source_selection")
                if selection and any(dimensions[name] != selection[name] for name in dimensions):
                    raise ProbeFailure("SOURCE_DIMENSIONS_CHANGED")
                dimensions["source_selection"] = selection
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe,crypto", "-i", str(output),
                "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=True, timeout=180, start_new_session=True)
            with output.open("rb") as content:
                digest = hashlib.file_digest(content, "sha256").hexdigest()
            self.client.headers["Authorization"] = "Bearer " + self.other_token
            try:
                owner_safe = all(self.client.get(path).status_code in {403, 404} for path in
                    ("/tasks/" + final["id"], "/files/" + file_id + "/download"))
                if not owner_safe:
                    raise ProbeFailure("OWNER_CHECK_FAILED")
            finally:
                self.client.headers["Authorization"] = "Bearer " + self.owner_token
            self.progress["checks"]["owner_isolation"] = True
            if self.data("POST", "/tasks/" + final["id"] + "/cancel")["status"] != "completed":
                raise ProbeFailure("COMPLETED_STATE_CHANGED")
            self.progress["checks"]["completed_state"] = True
            return {"file_bytes": size, "duration": duration, "tracks": tracks, "file_sha256": digest,
                    "full_decode_verified": True, **dimensions}
        finally:
            output.unlink(missing_ok=True)

    def attempt(self, sample, capability, metadata=None):
        key = sample["sample_id"] + ":" + capability
        self.context(key)
        self.progress["pending"] = {"sample_id": sample["sample_id"], "capability": capability}
        self.persist()
        result = {"sample_id": sample["sample_id"], "platform": sample["platform"], "capability": capability, "started_at": timestamp()}
        started = time.monotonic()
        value = None
        try:
            if capability == "metadata":
                value = self.data("POST", "/videos/parse", json={"url": sample["url"]})
                identities = {sample["video_id"]}
                if sample.get("allow_default_first_part") and sample["platform"] == "bilibili":
                    identities.add(sample["video_id"] + "_p1")
                if value["platform_video_id"] not in identities or value["platform"] != sample["platform"]:
                    raise ProbeFailure("VIDEO_ID_CHANGED")
                if not isinstance(value["duration"], (int, float)) or not 0 < value["duration"] <= 1200:
                    raise ProbeFailure("VIDEO_DURATION_OUTSIDE_TEST_BUDGET")
                result["metadata"] = {name: value[name] for name in ("duration", "uploader", "download_options")}
            else:
                available = {option["id"] for option in metadata["download_options"]}
                choices = ["audio_mp3"] if capability == "audio" else ["video_480_mp4", "video_720_mp4", "video_1080_mp4", "video_source_mp4"]
                preset = next((choice for choice in choices if choice in available), None)
                if preset is None:
                    raise ProbeFailure("PRESET_UNAVAILABLE")
                task_id = self.create(sample["url"], preset)
                final = self.completed(task_id)
                if final["status"] != "completed":
                    raise ProbeFailure(final.get("error_code") or "DOWNLOAD_FAILED")
                final["id"] = task_id
                result.update(self.verify_file(final, metadata, capability))
                result["task_id"], result["preset"] = task_id, preset
            result["code"] = "OK"
        except InterruptedError:
            raise
        except Exception as error:
            result["code"] = error.code if isinstance(error, ProbeFailure) else "ACCEPTANCE_FAILED"
            result["exception_type"] = type(error).__name__
        events = self.trace()
        if result["code"] == "OK" and not any(e["kind"] == "http_attempt" for e in events):
            result["code"] = "TELEMETRY_MISSING"
        elif any(e["kind"] == "http_attempt" for e in events):
            self.progress["checks"]["telemetry"] = True
        result.update(http_attempts=sum(e["kind"] == "http_attempt" for e in events),
                      source_mode=next((e["source_mode"] for e in reversed(events) if e["kind"] == "selection"), None),
                      finished_at=timestamp(), seconds=round(time.monotonic() - started, 3))
        self.progress["observations"].append(result)
        self.progress["pending"] = None
        self.persist()
        return value if result["code"] == "OK" else None

    def cancellation_probe(self):
        self.context("safety_cancellation")
        self.progress["safety_pending"] = {}
        self.persist()
        try:
            task_id = self.create("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "video_480_mp4", "safety_pending")
            directory = self.state / "storage/tmp" / task_id
            deadline = time.monotonic() + 90
            groups = set()
            while time.monotonic() < deadline:
                task = self.data("GET", "/tasks/" + task_id)
                if task["status"] in TERMINAL:
                    raise ProbeFailure(task.get("error_code") or "CANCEL_TRANSFER_NOT_OBSERVED")
                size = 0
                for path in directory.rglob("*"):
                    try:
                        if path.is_file() and not path.name.startswith("."):
                            size += path.stat().st_size
                    except FileNotFoundError:
                        pass
                if size > 65536:
                    groups = process_groups(self.processes[0].pid) - {self.processes[0].pid}
                    break
                time.sleep(.25)
            requested = time.monotonic()
            self.data("POST", "/tasks/" + task_id + "/cancel")
            final = self.completed(task_id, 20)
            if not groups or final["status"] != "cancelled" or directory.exists():
                raise ProbeFailure("CANCEL_CLEANUP_FAILED")
            for group in groups:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    continue
                raise ProbeFailure("CANCEL_PROCESS_SURVIVED")
            self.progress["checks"].update(cancellation=True, cancellation_seconds=round(time.monotonic() - requested, 3))
        except InterruptedError:
            raise
        except Exception as error:
            self.progress["checks"]["cancellation"] = False
            self.progress["checks"]["cancellation_code"] = error.code if isinstance(error, ProbeFailure) else "ACCEPTANCE_FAILED"
        finally:
            pending = self.progress["safety_pending"]
            if pending.get("task_id"):
                self.data("POST", "/tasks/" + pending["task_id"] + "/cancel")
                self.completed(pending["task_id"], 20)
            self.progress["safety_pending"] = None
            self.persist()

    def recover_pending(self, key):
        pending = self.progress.get(key)
        if not pending:
            return
        task_id = pending.get("task_id")
        if not task_id and pending.get("quote_id"):
            try:
                task_id = self.data("POST", "/billing/tasks", json={"quote_id": pending["quote_id"]})["task_id"]
            except ProbeFailure as error:
                if error.code != "QUOTE_EXPIRED":
                    raise
        if task_id:
            self.data("POST", "/tasks/" + task_id + "/cancel")
            self.completed(task_id, 20)
        if key == "pending":
            self.progress["observations"].append({"sample_id": pending["sample_id"], "capability": pending["capability"],
                "code": "ACCEPTANCE_INTERRUPTED", "finished_at": timestamp(), "http_attempts": 0})
        else:
            self.progress["checks"].update(cancellation=False, cancellation_code="ACCEPTANCE_INTERRUPTED")
        self.progress[key] = None
        self.persist()

    def run(self):
        self.start()
        self.recover_pending("safety_pending")
        self.recover_pending("pending")
        if "cancellation" not in self.progress["checks"]:
            self.cancellation_probe()
        while True:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(self.progress["started_at"])).total_seconds()
            day = min(2, int(elapsed // 86400))
            known = {(row["sample_id"], row["capability"]): row for row in self.progress["observations"]}
            for sample in self.manifest["samples"]:
                if sample["scheduled_day"] > day:
                    continue
                if (sample["sample_id"], "video") in known:
                    continue
                metadata_row = known.get((sample["sample_id"], "metadata"))
                if metadata_row:
                    metadata = metadata_row.get("metadata")
                else:
                    metadata = self.attempt(sample, "metadata")
                if metadata is None:
                    for capability in ("audio", "video"):
                        if (sample["sample_id"], capability) not in known:
                            self.progress["observations"].append({"sample_id": sample["sample_id"], "platform": sample["platform"],
                                "capability": capability, "code": "SOURCE_METADATA_UNAVAILABLE", "finished_at": timestamp(), "http_attempts": 0})
                    self.persist()
                    continue
                for capability in ("audio", "video"):
                    if (sample["sample_id"], capability) not in known:
                        self.attempt(sample, capability, metadata)
                time.sleep(5)
            if elapsed >= 72 * 3600:
                self.progress["status"] = "observation_window_finished_release_gated"
                self.persist()
                break
            self.progress["status"] = "waiting_for_next_observation_day"
            self.persist()
            time.sleep(60)

    def run_quick(self):
        """Regression evidence for a trial update, separately from multiday eligibility."""
        samples = [sample for sample in self.manifest["samples"] if sample["video_id"] in QUICK_VIDEO_IDS]
        if {sample["video_id"] for sample in samples} != QUICK_VIDEO_IDS:
            raise ValueError("Incomplete trial regression corpus")
        self.progress.update(acceptance_mode="quick_trial", trial_eligible=False)
        self.start()
        self.recover_pending("safety_pending")
        self.recover_pending("pending")
        if "cancellation" not in self.progress["checks"]:
            self.cancellation_probe()
        known = {(row["sample_id"], row["capability"]): row for row in self.progress["observations"]}
        for sample in samples:
            metadata_row = known.get((sample["sample_id"], "metadata"))
            metadata = metadata_row.get("metadata") if metadata_row else self.attempt(sample, "metadata")
            for capability in ("audio", "video"):
                if (sample["sample_id"], capability) in known:
                    continue
                if metadata:
                    self.attempt(sample, capability, metadata)
                else:
                    self.progress["observations"].append({"sample_id": sample["sample_id"], "platform": sample["platform"],
                        "capability": capability, "code": "SOURCE_METADATA_UNAVAILABLE", "finished_at": timestamp(), "http_attempts": 0})
                    self.persist()
            time.sleep(5)
        rows = self.progress["observations"]
        self.progress["trial_eligible"] = (len(rows) == len(samples) * len(CAPABILITIES)
            and all(row["code"] == "OK" for row in rows)
            and all(self.progress["checks"].get(name) is True for name in REQUIRED_SAFETY[:5]))
        self.progress["status"] = "quick_trial_passed" if self.progress["trial_eligible"] else "quick_trial_failed"
        self.persist()

    def close(self):
        for process in reversed(self.processes):
            stop_process(process)
        for stream in self.logs:
            stream.close()
        self.listener.close()
        if hasattr(self, "client"):
            self.client.close()
        self.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "api", "worker", "run", "run-quick", "validate"])
    parser.add_argument("--state", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.mode == "validate":
        manifest = validate_manifest(json.loads(args.manifest.read_text()))
        print(json.dumps({"valid": True, "samples": {p: sum(s["platform"] == p for s in manifest["samples"]) for p in PLATFORMS}}))
        return
    if not args.state or args.state.is_symlink():
        parser.error("A private acceptance state directory is required")
    if args.mode == "prepare":
        prepare(args.state)
    elif args.mode in {"api", "worker"}:
        serve(args.state, args.mode)
    else:
        import fcntl
        with (args.state / "campaign.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            campaign = Campaign(args.state)
            def interrupt(signum, frame):
                raise InterruptedError("Acceptance interrupted")
            signal.signal(signal.SIGTERM, interrupt)
            try:
                if args.mode == "run-quick":
                    campaign.run_quick()
                else:
                    campaign.run()
            finally:
                campaign.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "code": error.code if isinstance(error, ProbeFailure) else "ACCEPTANCE_OPERATOR_FAILED"}))
        raise SystemExit(1)
