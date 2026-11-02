"""Real HTTP/PG/Redis/Celery Solo/rendering; explicitly synthetic ASR and LLM."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest
from sqlalchemy import text

from test_allowances_postgres import postgres
from test_tasks import redis_url
from test_clips import clip_video
from app.core.config import settings
from app.modules.auth.tokens import issue_token


def test_http_private_video_plan_export_sse_range_and_cached_delivery(postgres, redis_url, clip_video, tmp_path, monkeypatch):
    if not redis_url.startswith("unix://"):
        pytest.skip("Requires disposable Redis")
    sessions, _, _, user, _ = postgres
    with sessions.kw["bind"].connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    backend = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "clip-smoke-test-" + "x" * 48)
    wrapper = tmp_path / "clips_server.py"
    wrapper.write_text('''
import os, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.infrastructure.database import session as database
engine = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1,
    "options": "-c search_path=" + os.environ["CLIP_TEST_SCHEMA"] + " -c statement_timeout=2000"})
database.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
database.engine = engine
from app.infrastructure.queue.celery_app import celery_app
celery_app.conf.task_default_queue = os.environ["CLIP_TEST_QUEUE"]
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
from test_clips import Selector, Timing, TEXTS
from app.modules.transcript.data import make_transcript
from app.modules.transcript import providers
class SyntheticASR:
    available, mock, model = True, True, "mock-v1"
    async def transcribe(self, path, language):
        return make_transcript("zh", [{"start": i * 4, "end": i * 4 + 3, "text": t} for i,t in enumerate(TEXTS)])
providers.get_asr_provider = lambda: SyntheticASR()
from app.modules.clips import worker
def selector():
    value = Selector()
    value.name = "heuristic"
    return value
worker.get_llm_provider = selector
worker.timing_provider = lambda model=None: Timing()
if os.environ["CLIP_TEST_MODE"] == "worker":
    celery_app.worker_main(["worker", "--pool=solo", "-c", "1", "--loglevel=info",
        "--without-gossip", "--without-mingle", "--without-heartbeat"])
else:
    import uvicorn
    uvicorn.run("app.main:create_app", factory=True, fd=int(os.environ["CLIP_TEST_FD"]), log_level="warning")
''')
    unix_path = redis_url.removeprefix("unix://").split("?", 1)[0]
    environment = {**os.environ, "PYTHONPATH": str(backend), "PYTHONUNBUFFERED": "1", "APP_ENV": "development",
        "ALLOWANCES_ENABLED": "true", "AUTH_REQUIRED": "true", "WECHAT_MP_MODE": "mock", "CLIPS_ENABLED": "true",
        "ASR_PROVIDER": "mock", "LLM_PROVIDER": "heuristic", "MAX_FILE_SIZE_MB": "32",
        "CLIPS_TIMING_API_BASE_URL": "https://example.com/v1", "CLIPS_TIMING_API_KEY": "explicit-test-only",
        "CLIPS_TIMING_MODEL": "timestamp-fixture", "CLIPS_EXPORT_MAX_TEMP_MB": "16",
        "STORAGE_LOCAL_PATH": str(tmp_path / "storage"), "REDIS_URL": redis_url,
        "CELERY_BROKER_URL": "redis+socket://" + unix_path + "?virtual_host=0", "CELERY_RESULT_BACKEND": "cache+memory://",
        "CLIP_TEST_SCHEMA": schema, "CLIP_TEST_QUEUE": "clips-smoke-" + schema, "JWT_SECRET_KEY": settings.JWT_SECRET_KEY}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    processes, logs = [], []
    try:
        for mode in ("worker", "api"):
            log = (tmp_path / (mode + ".log")).open("w")
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, str(wrapper)], env={**environment,
                "CLIP_TEST_MODE": mode, "CLIP_TEST_FD": str(listener.fileno())},
                stdout=log, stderr=log, start_new_session=True, cwd=backend,
                pass_fds=(listener.fileno(),) if mode == "api" else ()))
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=3, trust_env=False,
            headers={"Authorization": "Bearer " + issue_token(user.id, user.auth_version)}) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert all(x.poll() is None for x in processes), "Isolated services exited"
                time.sleep(.1)
            else:
                pytest.fail("Isolated API not ready")

            def data(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                assert response.is_success, response.text
                return response.json()["data"]

            def complete(task_id):
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    item = data("GET", f"/tasks/{task_id}")
                    if item["status"] in {"completed", "failed", "cancelled", "expired"}:
                        assert item["status"] == "completed", item
                        return item["result_payload"]
                    assert all(x.poll() is None for x in processes), "Worker/API exited"
                    time.sleep(.1)
                pytest.fail("Solo task did not complete within 45 seconds")

            upload = data("POST", "/uploads", json={"filename": "fixture.mp4", "size_bytes": len(clip_video)})
            data("PUT", f"/uploads/{upload['id']}/content", content=clip_video, headers={"Content-Type": "application/octet-stream"})
            probe = data("POST", f"/uploads/{upload['id']}/complete")
            complete(probe["task_id"])
            quote = data("POST", "/clips/plans/quotes", json={"upload_id": upload["id"],
                "strategy": {"count": 2, "min_seconds": 1, "max_seconds": 10}})
            plan_task = data("POST", "/billing/tasks", json={"quote_id": quote["quote_id"]})
            result = complete(plan_task["task_id"])
            plan = data("GET", f"/clips/plans/{result['clip_plan_id']}")
            assert len(plan["candidates"]) == 2 and plan["mock"] is True
            balance = data("GET", "/billing/allowances")["balances"]
            assert balance["ai_generations"]["available"] == 4
            assert balance["asr_seconds"]["available"] == 1800 - 12
            quote = data("POST", "/clips/exports/quotes", json={"plan_id": plan["id"],
                "revision": 1, "candidate_ids": [x["id"] for x in plan["candidates"]]})
            assert quote["maximum"]["ai_generations"] == quote["maximum"]["asr_seconds"] == 0
            export_task = data("POST", "/billing/tasks", json={"quote_id": quote["quote_id"]})
            exported = complete(export_task["task_id"])
            batch = data("GET", f"/clips/exports/{exported['clip_export_id']}")
            assert len(batch["items"]) == 2 and all(x["download_available"] for x in batch["items"])
            assert data("GET", "/billing/allowances")["balances"] == balance
            stream = client.get(f"/tasks/{export_task['task_id']}/events")
            assert stream.status_code == 200 and "event: progress" in stream.text and '"seq":' in stream.text
            file_id = batch["items"][0]["file_id"]
            access = data("POST", f"/files/{file_id}/download/access")
            assert access
            response = client.get(f"/files/{file_id}/download", headers={"Range": "bytes=0-99"})
            assert response.status_code == 206 and len(response.content) == 100
            data("DELETE", f"/uploads/{upload['id']}")
            assert data("GET", f"/transcriptions/{result['transcript_id']}")["content_text"]
            assert data("GET", f"/clips/plans/{plan['id']}")["source_required"]
            again = data("POST", "/clips/exports/quotes", json={"plan_id": plan["id"],
                "candidate_ids": [x["id"] for x in plan["candidates"]]})
            repeated = data("POST", "/billing/tasks", json={"quote_id": again["quote_id"]})
            assert complete(repeated["task_id"])["clip_export_id"] == batch["id"]
    finally:
        listener.close()
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        for log in logs:
            log.close()
