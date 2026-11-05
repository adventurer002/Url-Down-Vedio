"""Fail-closed acceptance statistics, archive safety and interrupted-run replay."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import Mock

import pytest

from scripts import acceptance_e as e
from deploy import prepare_acceptance_e as installer


@pytest.fixture
def manifest():
    return json.loads((e.BACKEND / "acceptance-e-samples.json").read_text())


def observations(manifest):
    return [{"sample_id": s["sample_id"], "capability": cap, "code": "OK", "http_attempts": 1,
             "source_mode": "independent_audio" if cap == "audio" else "video",
             "finished_at": f"2026-11-{4+s['scheduled_day']:02}T14:00:00+00:00"}
            for s in manifest["samples"] for cap in e.CAPABILITIES]


def test_real_manifest_is_canonical_and_preregistered(manifest):
    assert e.validate_manifest(manifest) == manifest
    assert {p: sum(s["platform"] == p for s in manifest["samples"]) for p in e.PLATFORMS} == {
        "bilibili": 30, "youtube": 30, "douyin": 1}
    assert manifest["test_membership_authorized"]


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "private", "schedule", "identity"])
def test_manifest_rejects_ambiguous_unbounded_or_nonplatform_input(manifest, mutation):
    if mutation == "duplicate":
        manifest["samples"].append(deepcopy(manifest["samples"][0]))
    elif mutation in {"foreign", "private"}:
        manifest["samples"][0]["url"] = "https://example.com/video/1" if mutation == "foreign" else "http://127.0.0.1/video/1"
    elif mutation == "schedule":
        manifest["samples"][0]["scheduled_day"] = 4
    else:
        row = next(s for s in manifest["samples"] if s["platform"] == "youtube")
        row["video_id"] = "wrong"
    with pytest.raises(Exception):
        e.validate_manifest(manifest)


def test_incomplete_corpus_and_missing_safety_never_pass(manifest):
    result = e.summarize(manifest, observations(manifest), elapsed_hours=73, safety={})
    assert not any(v["release_eligible"] for v in result["platforms"].values())
    result = e.summarize(manifest, observations(manifest), elapsed_hours=73, safety=dict.fromkeys(e.REQUIRED_SAFETY, True))
    assert result["platforms"]["bilibili"]["release_eligible"]
    assert result["platforms"]["youtube"]["release_eligible"]
    assert not result["platforms"]["douyin"]["release_eligible"]


@pytest.mark.parametrize("hours,one_date", [(71.9, False), (73, True)])
def test_elapsed_window_and_real_observation_dates_both_required(manifest, hours, one_date):
    rows = observations(manifest)
    if one_date:
        for row in rows:
            row["finished_at"] = "2026-11-04T14:00:00+00:00"
    result = e.summarize(manifest, rows, elapsed_hours=hours, safety=dict.fromkeys(e.REQUIRED_SAFETY, True))
    assert not result["platforms"]["youtube"]["release_eligible"]


def test_cooldown_is_logical_failure_but_not_a_fresh_network_attempt(manifest):
    rows = observations(manifest)
    for row in rows:
        if row["sample_id"] == "bilibili_BV1g2JX6nESP":
            row.update(code="SOURCE_COOLDOWN", http_attempts=0)
    output = e.summarize(manifest, rows, elapsed_hours=73, safety=dict.fromkeys(e.REQUIRED_SAFETY, True))
    metadata = output["platforms"]["bilibili"]["capabilities"]["metadata"]
    assert metadata["success"] == 29 and metadata["registered_denominator"] == metadata["attempted_logical"] == 30
    assert metadata["fresh_attempted"] == metadata["fresh_success"] == 29
    assert metadata["errors"] == {"SOURCE_COOLDOWN": 1}
    assert output["platforms"]["douyin"]["capabilities"]["metadata"]["registered_denominator"] == 1


def test_duplicate_or_unregistered_results_are_rejected(manifest):
    rows = observations(manifest)
    with pytest.raises(ValueError):
        e.summarize(manifest, rows + [rows[0]], elapsed_hours=73, safety={})
    with pytest.raises(ValueError):
        e.summarize(manifest, [{**rows[0], "sample_id": "unknown"}], elapsed_hours=73, safety={})


@pytest.mark.parametrize("code,check", [("OWNER_CHECK_FAILED", "owner_isolation"),
    ("COMPLETED_STATE_CHANGED", "completed_state"), ("TELEMETRY_MISSING", "telemetry")])
def test_later_success_never_erases_recorded_safety_failure(manifest, code, check):
    rows = observations(manifest)
    rows[1]["code"] = code
    result = e.summarize(manifest, rows, elapsed_hours=73, safety=dict.fromkeys(e.REQUIRED_SAFETY, True))
    assert result["safety"][check] is False
    assert not any(platform["release_eligible"] for platform in result["platforms"].values())


def bundle(files, extra=None):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        if extra:
            archive.addfile(extra)
    return result.getvalue()


@pytest.fixture
def public_files():
    files = {name: b"public test source" for name in installer.REQUIRED}
    files["backend/acceptance-e-samples.json"] = json.dumps({"test_membership_authorized": True, "duration_hours": 72}).encode()
    return files


@pytest.mark.parametrize("invalid", ["hash", "traversal", "symlink", "private", "authorization"])
def test_root_setup_checks_hash_archive_paths_and_explicit_test_grant(public_files, invalid):
    extra = None
    if invalid == "traversal":
        public_files["backend/app/../../escape.py"] = b"unsafe"
    elif invalid == "symlink":
        extra = tarfile.TarInfo("backend/app/link.py")
        extra.type, extra.linkname = tarfile.SYMTYPE, "/etc/passwd"
    elif invalid == "private":
        public_files["backend/.env"] = b"must never ship"
    elif invalid == "authorization":
        public_files["backend/acceptance-e-samples.json"] = b'{"test_membership_authorized": false, "duration_hours":72}'
    content = bundle(public_files, extra)
    digest = "0" * 64 if invalid == "hash" else hashlib.sha256(content).hexdigest()
    with pytest.raises(ValueError):
        installer.verify_bundle(content, digest)


def test_valid_public_bundle_verification_has_no_root_side_effects(public_files):
    content = bundle(public_files)
    assert installer.verify_bundle(content, hashlib.sha256(content).hexdigest()) == public_files


@pytest.mark.parametrize("expired", [False, True])
def test_interrupted_confirmation_reuses_quote_and_never_creates_new_quote(expired):
    campaign = object.__new__(e.Campaign)
    campaign.progress = {"pending": {"sample_id": "youtube_sample", "capability": "video", "quote_id": "original"},
                         "observations": [], "checks": {}}
    def data(method, path, **kwargs):
        if path == "/billing/tasks":
            assert kwargs["json"] == {"quote_id": "original"}
            if expired:
                raise e.ProbeFailure("QUOTE_EXPIRED")
            return {"task_id": "same-task"}
        assert path == "/tasks/same-task/cancel"
        return {"status": "cancelled"}
    campaign.data = Mock(side_effect=data)
    campaign.completed = Mock()
    campaign.persist = Mock()
    campaign.recover_pending("pending")
    assert campaign.progress["pending"] is None
    assert campaign.progress["observations"][0]["code"] == "ACCEPTANCE_INTERRUPTED"
    assert campaign.completed.call_count == (0 if expired else 1)


def test_atomic_report_is_json_and_has_requested_mode(tmp_path):
    import os
    path = tmp_path / "report.json"
    previous = os.umask(0o077)
    try:
        e.atomic_json(path, {"status": "waiting"}, 0o644)
    finally:
        os.umask(previous)
    assert json.loads(path.read_text()) == {"status": "waiting"}
    assert path.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.iterdir()) == [path]


def test_child_telemetry_records_counts_without_urls_or_credentials(tmp_path, monkeypatch):
    import os
    directory = tmp_path / "trace"
    directory.mkdir()
    (directory / "current.json").write_text('{"operation":"sample:audio"}')
    environment = {**os.environ, "VIDEOHUB_E_TRACE_DIRECTORY": str(directory), "PYTHONPATH":
        str(e.BACKEND / "scripts/acceptance_e_trace") + os.pathsep + str(e.BACKEND)}
    command = "import urllib.request, urllib.error\ntry: urllib.request.OpenerDirector().open('invalid://secret-private')\nexcept urllib.error.URLError: pass"
    result = subprocess.run([sys.executable, "-c", command], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    row = json.loads((directory / "events.jsonl").read_text())
    assert row["operation"] == "sample:audio" and row["kind"] == "http_attempt"
    assert not {"url", "headers", "proxy", "cookie", "authorization"} & row.keys()


def test_real_isolated_startup_auth_and_audited_membership(postgres, redis_url, tmp_path):
    """Start the actual API/worker and use the same public login and grant flow."""
    import os
    from uuid import uuid4
    from sqlalchemy import text
    from app.core.config import settings
    if not redis_url.startswith("unix://"):
        pytest.skip("Requires disposable Redis")
    sessions = postgres[0]
    schema = "videohub_e_" + uuid4().hex
    with sessions.kw["bind"].begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    state = tmp_path / "state"
    state.mkdir()
    (state / "trace").mkdir()
    e.atomic_json(state / "trace/current.json", {"operation": "setup"})
    unix_path = redis_url.removeprefix("unix://").split("?", 1)[0]
    config = {"DATABASE_URL": settings.DATABASE_URL, "REDIS_URL": redis_url,
        "CELERY_BROKER_URL": "redis+socket://" + unix_path + "?virtual_host=0", "CELERY_RESULT_BACKEND": "cache+memory://",
        "APP_ENV": "development", "AUTH_REQUIRED": "true", "ALLOWANCES_ENABLED": "true", "WECHAT_MP_MODE": "mock",
        "WECHAT_MP_STATIC_QR_URL": "", "WECHAT_MP_TOKEN": uuid4().hex, "JWT_SECRET_KEY": uuid4().hex * 2,
        "VIDEO_EGRESS_MODE": "platform", "YTDLP_PROXY": "", "YOUTUBE_PROXY": "http://127.0.0.1:9",
        "YTDLP_JS_RUNTIME": "disabled", "ASR_PROVIDER": "disabled", "LLM_PROVIDER": "heuristic", "CLIPS_ENABLED": "false",
        "STORAGE_LOCAL_PATH": str(state / "storage"), "VIDEOHUB_E_SCHEMA": schema, "VIDEOHUB_E_QUEUE": uuid4().hex,
        "VIDEOHUB_E_REPORT": str(tmp_path / "report.json"), "VIDEOHUB_E_TRACE_DIRECTORY": str(state / "trace"),
        "PYTHONPATH": str(e.BACKEND / "scripts/acceptance_e_trace") + os.pathsep + str(e.BACKEND)}
    e.atomic_json(state / "private.json", config)
    program = '''
import os, pwd, sys
from pathlib import Path
from types import SimpleNamespace
from scripts import acceptance_e as e
pwd.getpwnam = lambda name: SimpleNamespace(pw_uid=os.getuid())
e.process_groups = lambda parent: {parent}
campaign = e.Campaign(Path(sys.argv[1]))
try:
    campaign.start()
    balance = campaign.data("GET", "/billing/allowances")
    assert balance["member"]
    audit = campaign.progress["membership_audit"]["audit_id"]
    first = campaign.data("GET", "/auth/me")["id"]
    again = campaign.login("owner")
    assert again["user"]["id"] == first
    assert campaign.progress["membership_audit"]["audit_id"] == audit
finally:
    campaign.close()
'''
    try:
        result = subprocess.run([sys.executable, "-c", program, str(state)], cwd=e.BACKEND,
            env={**os.environ, "PYTHONPATH": str(e.BACKEND)}, capture_output=True, text=True, timeout=55)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["membership_audit"]["payment_verified"] is False
        assert report["membership_audit"]["isolated_only"]
        assert not report["production_application_changed"]
    finally:
        with sessions.kw["bind"].begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))


from test_allowances_postgres import postgres
from test_tasks import redis_url


def test_empty_but_configured_production_redis_databases_are_excluded():
    assert e.production_redis_databases([
        "redis://user:private@redis.example:6379/3", "rediss://redis.example:6380/8",
        "redis://redis.example/", "cache+memory://",
    ]) == {0, 1, 2, 3, 8}
    with pytest.raises(ValueError):
        e.production_redis_databases(["redis://redis.example/not-a-database"])


@pytest.mark.parametrize("failed", [False, True])
def test_quick_trial_evidence_keeps_multiday_gate_separate(manifest, failed, monkeypatch):
    campaign = object.__new__(e.Campaign)
    campaign.manifest = manifest
    campaign.progress = {"observations": [], "checks": dict.fromkeys(e.REQUIRED_SAFETY[:5], True)}
    campaign.start = Mock()
    campaign.persist = Mock()
    campaign.recover_pending = Mock()
    monkeypatch.setattr(e.time, "sleep", lambda _: None)

    def attempt(sample, capability, metadata=None):
        code = "PRESET_UNAVAILABLE" if failed and capability == "video" else "OK"
        campaign.progress["observations"].append({"sample_id": sample["sample_id"], "capability": capability,
            "code": code, "finished_at": "2026-11-05T00:00:00+00:00", "http_attempts": 1})
        return {"duration": 1}

    campaign.attempt = attempt
    campaign.run_quick()
    assert campaign.progress["trial_eligible"] is not failed
    assert len(campaign.progress["observations"]) == 18
    result = e.summarize(manifest, campaign.progress["observations"], elapsed_hours=1,
                         safety=campaign.progress["checks"])
    assert not any(platform["release_eligible"] for platform in result["platforms"].values())


def test_browser_runtime_copy_preserves_valid_library_links_and_omits_absent_docs(tmp_path):
    source = tmp_path / "source"
    (source / "lib").mkdir(parents=True)
    (source / "lib/libfixture.so.1.0").write_bytes(b"verified-shared-library")
    (source / "lib/libfixture.so.1.0").chmod(0o755)
    (source / "lib/libfixture.so.1").symlink_to("libfixture.so.1.0")
    (source / "README").symlink_to("../absent-documentation/README")
    target = tmp_path / "runtime"
    installer.copy_runtime(source, target)
    assert (target / "lib/libfixture.so.1").read_bytes() == b"verified-shared-library"
    assert not (target / "lib/libfixture.so.1").is_symlink()
    assert (target / "lib/libfixture.so.1").stat().st_mode & 0o111
    assert not (target / "README").exists()
    assert installer.runtime_digest({"runtime": source}) == installer.runtime_digest({"runtime": target})
