"""Real HTTP + Celery Solo smoke test, using disposable PostgreSQL schema/Redis socket."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from test_allowances_postgres import postgres
from test_tasks import redis_url
from test_uploads import media_samples
from app.core.config import settings
from app.modules.auth.tokens import issue_token


@pytest.mark.parametrize("filename,mode", [("sample.mp3", "legacy"), ("sample.m4a", "legacy"),
                                           ("sample.mp3", "staged"), ("sample.mp3", "capacity")])
def test_http_upload_to_solo_worker_and_ai_delivery(postgres, redis_url, media_samples, tmp_path, monkeypatch, filename, mode):
    staged = mode != "legacy"
    if not redis_url.startswith("unix://"):
        pytest.skip("Celery smoke requires the disposable local Redis fixture")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "upload-smoke-test-key-" + "x" * 40)
    sessions, _, _, user, _ = postgres
    engine = sessions.kw["bind"]
    from sqlalchemy import text
    with engine.connect() as connection:
        schema_name = connection.scalar(text("SELECT current_schema()"))
    backend_dir = Path(__file__).resolve().parents[1]
    wrapper = tmp_path / "server.py"
    wrapper.write_text('''
import asyncio
import json
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.infrastructure.database import session as database
engine = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1,
    "options": "-c search_path=" + os.environ["UPLOAD_TEST_SCHEMA"] + " -c statement_timeout=1500"})
database.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
database.engine = engine
from app.infrastructure.queue.celery_app import celery_app
celery_app.conf.task_default_queue = os.environ["UPLOAD_TEST_QUEUE"]
from app.modules.tasks.dispatch import LEGACY_QUEUES, PHASE_QUEUES
for routes in (LEGACY_QUEUES, PHASE_QUEUES):
    for key, queue in routes.items():
        routes[key] = os.environ["UPLOAD_TEST_QUEUE"] + "." + queue
if os.environ["UPLOAD_TEST_CAPACITY"] == "1":
    from app.infrastructure.timing import stage_timer
    from app.modules.download.adapter import YtDlpDownloadAdapter
    from app.modules.video import resolver

    async def fixed_link(url):
        return url

    async def fixture_transfer(self, payload, directory, **controls):
        with stage_timer("source_transfer"):
            await asyncio.sleep(1.5)
            (directory / "source_0.mp3").write_bytes(Path(os.environ["UPLOAD_TEST_MP3"]).read_bytes())
        manifest = directory / "download-sources.json"
        manifest.write_text(json.dumps({"sources": ["source_0.mp3"], "codecs": [{}]}))
        return manifest

    resolver.prepare_source_link = fixed_link
    YtDlpDownloadAdapter.download = fixture_transfer
mode = os.environ["UPLOAD_TEST_MODE"]
if mode != "api":
    queue = os.environ["UPLOAD_TEST_QUEUE"]
    if mode != "worker":
        queue += "." + mode.split("-")[0]
    celery_app.worker_main(["worker", "--pool=solo", "-c", "1", "--loglevel=info", "-Q", queue, "-n", mode + "@%h",
        "--without-gossip", "--without-mingle", "--without-heartbeat"])
else:
    import uvicorn
    uvicorn.run("app.main:create_app", factory=True, fd=int(os.environ["UPLOAD_TEST_FD"]), log_level="warning")
''')
    sample = tmp_path / "source.mp3"
    sample.write_bytes(media_samples["sample.mp3"])
    unix_path = redis_url.removeprefix("unix://").split("?", 1)[0]
    environment = {**os.environ, "PYTHONPATH": str(backend_dir), "PYTHONUNBUFFERED": "1",
        "APP_ENV": "development", "ALLOWANCES_ENABLED": "true", "AUTH_REQUIRED": "true",
        "STAGED_TASKS_ENABLED": "true" if staged else "false",
        "WECHAT_MP_MODE": "mock", "ASR_PROVIDER": "mock", "LLM_PROVIDER": "heuristic",
        "ASR_MOCK_TEXT": "缓存键应包含模型版本，数据库事务保证额度只结算一次。",
        "MAX_FILE_SIZE_MB": "32", "STORAGE_LOCAL_PATH": str(tmp_path / "storage"),
        "REDIS_URL": redis_url, "CELERY_BROKER_URL": "redis+socket://" + unix_path + "?virtual_host=0",
        "CELERY_RESULT_BACKEND": "cache+memory://", "UPLOAD_TEST_SCHEMA": schema_name,
        "UPLOAD_TEST_QUEUE": "uploads-smoke-" + schema_name,
        "JWT_SECRET_KEY": settings.JWT_SECRET_KEY, "UPLOAD_TEST_CAPACITY": "1" if mode == "capacity" else "0",
        "UPLOAD_TEST_MP3": str(sample)}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    processes, logs = [], []
    try:
        modes = ("control", "media", "asr", "llm", "api") if staged else ("worker", "api")
        if mode == "capacity":
            modes = ("control", "media", "media-2", "asr", "llm", "render", "api")
        for instance in modes:
            log = (tmp_path / f"{instance}.log").open("w")
            logs.append(log)
            process = subprocess.Popen([sys.executable, str(wrapper)], env={**environment,
                "UPLOAD_TEST_MODE": instance, "UPLOAD_TEST_FD": str(listener.fileno())},
                stdout=log, stderr=log, start_new_session=True, cwd=backend_dir,
                pass_fds=(listener.fileno(),) if instance == "api" else ())
            processes.append(process)
        headers = {"Authorization": "Bearer " + issue_token(user.id, user.auth_version)}
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers=headers, timeout=3, trust_env=False) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if client.get("/health/live").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert all(p.poll() is None for p in processes), "Isolated API/worker exited"
                time.sleep(.1)
            else:
                pytest.fail("Isolated API failed to start")

            def data(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                assert response.is_success, response.text
                return response.json()["data"]

            def completed(task_id):
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    task = data("GET", f"/tasks/{task_id}")
                    if task["status"] in {"completed", "failed", "cancelled", "expired"}:
                        assert task["status"] == "completed", task
                        return task
                    assert all(p.poll() is None for p in processes), "Isolated API/worker exited"
                    time.sleep(.1)
                pytest.fail("Celery Solo did not finish the test task within 25 seconds")

            content = media_samples[filename]
            upload = data("POST", "/uploads", json={"filename": filename, "size_bytes": len(content)})
            identifier = upload["id"]
            data("PUT", f"/uploads/{identifier}/content", content=content,
                 headers={"Content-Type": "application/octet-stream"})
            probe = data("POST", f"/uploads/{identifier}/complete")
            completed(probe["task_id"])
            assert data("GET", f"/uploads/{identifier}")["status"] == "ready"
            quote = data("POST", "/billing/quotes", json={"upload_id": identifier, "action": "ai_mindmap", "language": "zh"})
            task = data("POST", "/billing/tasks", json={"quote_id": quote["quote_id"]})
            result = completed(task["task_id"])["result_payload"]
            assert data("GET", f"/transcriptions/{result['transcript_id']}")["source_type"] == "asr"
            assert data("GET", f"/ai/results/{result['result_id']}")["result_type"] == "mindmap"
            assert data("GET", f"/ai/results/{result['summary_result_id']}")["result_type"] == "summary"
            stream = client.get(f"/tasks/{task['task_id']}/events")
            assert stream.status_code == 200 and "event: progress" in stream.text and '"seq":' in stream.text
            if mode == "capacity":
                run_capacity_wave(sessions, user, result["transcript_id"], client.base_url, tmp_path, modes)
                return
            data("DELETE", f"/uploads/{identifier}")
            assert data("GET", f"/transcriptions/{result['transcript_id']}")["content_text"]
            before = data("GET", "/billing/allowances")["balances"]
            text_quote = data("POST", "/billing/quotes", json={"transcript_id": result["transcript_id"],
                "action": "ai_summary", "language": "en"})
            assert text_quote["maximum"]["asr_seconds"] == 0
            text_task = data("POST", "/billing/tasks", json={"quote_id": text_quote["quote_id"]})
            text_result = completed(text_task["task_id"])["result_payload"]
            summary = data("GET", f"/ai/results/{text_result['result_id']}")
            assert summary["transcript_id"] == result["transcript_id"] and summary["output_language"] == "en"
            after = data("GET", "/billing/allowances")["balances"]
            assert after["asr_seconds"] == before["asr_seconds"]
            assert after["ai_generations"]["available"] == before["ai_generations"]["available"] - 1
            repeated = data("POST", "/billing/quotes", json={"transcript_id": result["transcript_id"],
                "action": "ai_summary", "language": "en"})
            assert repeated["maximum"]["ai_generations"] == 0
            again = data("POST", "/billing/tasks", json={"quote_id": repeated["quote_id"]})
            assert completed(again["task_id"])["result_payload"]["result_id"] == text_result["result_id"]
            assert data("GET", "/billing/allowances")["balances"] == after
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        listener.close()
        for log in logs:
            log.close()


def run_capacity_wave(sessions, user, transcript_id, base_url, tmp_path, modes):
    from app.modules.admin.service import AdminService
    from app.modules.auth.repository import UserRepository
    from app.modules.video.parsers.base import ParsedVideo
    from app.modules.video.repository import VideoRepository
    users = [user] + [UserRepository(sessions).subscription(f"capacity-{number}", True, login=True)
                      for number in range(1, 10)]
    for number, account in enumerate(users):
        AdminService(sessions).grant_user(None, account.id, "monthly", "Isolated capacity fixture",
                                          f"capacity-{number}", f"req_capacity_{number}", operator="pytest")
    tokens = {str(number): issue_token(account.id, account.auth_version) for number, account in enumerate(users)}
    waves = []
    for population in (1, 2, 5, 10):
        jobs = []
        for number in range(population):
            label = f"d{population}u{number}"
            url = f"https://www.youtube.com/watch?v={label}"
            VideoRepository(sessions).get_or_create(ParsedVideo("youtube", label, label, duration=5), url)
            jobs.append({"id": label, "account": str(number), "cache": "cold", "delivery": "local",
                         "quote": {"action": "download", "url": url, "preset_id": "audio_mp3"}})
        if population == 2:
            jobs.append({"id": "summary", "account": "0", "cache": "cold", "quote": {
                "action": "ai_summary", "transcript_id": transcript_id, "language": "en"}})
        if population == 5:
            jobs.append({**waves[0]["jobs"][0], "id": "cached", "cache": "hit"})
        waves.append({"users": population, "jobs": jobs})

    manifest, token_file, output = (tmp_path / f"{name}.json" for name in ("waves", "tokens", "report"))
    manifest.write_text(json.dumps(waves))
    token_file.write_text(json.dumps(tokens))
    script = Path(__file__).resolve().parents[1] / "scripts" / "acceptance_d.py"
    command = [sys.executable, str(script), str(manifest), "--api", str(base_url).split("/api/v1")[0],
               "--tokens", str(token_file), "--output", str(output), "--environment", "synthetic",
               "--baseline-seconds", "0", "--timeout", "25", "--poll-interval", ".2"]
    for instance in modes:
        if instance != "api":
            command.extend(["--worker-log", str(tmp_path / f"{instance}.log")])
    run = subprocess.run(command, capture_output=True, text=True, timeout=75)
    assert run.returncode == 0, run.stderr
    evidence = output.read_text()
    assert all(token not in evidence for token in tokens.values())
    report = json.loads(evidence)
    assert report["complete"]
    assert [wave["users"] for wave in report["waves"]] == [1, 2, 5, 10]
    for wave in report["waves"]:
        metrics = wave["metrics"]
        assert metrics["completed_with_checks"] == metrics["cache_expectations_matched"] == len(wave["jobs"]), wave
        assert metrics["cold_work_observed"] == metrics["cold_expected"], wave
    mixed = report["waves"][1]
    assert mixed["metrics"]["dispatch_to_claim_by_phase"]["render"]["count"] == 2, mixed
    first, second = [next(stage for stage in job["work"] if stage["stage"] == "source_transfer") for job in mixed["jobs"][:2]]
    assert first["pid"] != second["pid"], mixed
    assert max(first["started_unix_ms"], second["started_unix_ms"]) < min(
        first["started_unix_ms"] + first["duration_ms"], second["started_unix_ms"] + second["duration_ms"]), mixed
    assert report["waves"][2]["jobs"][-1]["result_cache_hit"] is True
