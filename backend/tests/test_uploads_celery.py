"""Real HTTP + Celery Solo smoke test, using disposable PostgreSQL schema/Redis socket."""
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


@pytest.mark.parametrize("filename", ["sample.mp3", "sample.m4a"])
def test_http_upload_to_solo_worker_and_ai_delivery(postgres, redis_url, media_samples, tmp_path, monkeypatch, filename):
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
import os
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
if os.environ["UPLOAD_TEST_MODE"] == "worker":
    celery_app.worker_main(["worker", "--pool=solo", "-c", "1", "--loglevel=info",
        "--without-gossip", "--without-mingle", "--without-heartbeat"])
else:
    import uvicorn
    uvicorn.run("app.main:create_app", factory=True, fd=int(os.environ["UPLOAD_TEST_FD"]), log_level="warning")
''')
    unix_path = redis_url.removeprefix("unix://").split("?", 1)[0]
    environment = {**os.environ, "PYTHONPATH": str(backend_dir), "PYTHONUNBUFFERED": "1",
        "APP_ENV": "development", "ALLOWANCES_ENABLED": "true", "AUTH_REQUIRED": "true",
        "WECHAT_MP_MODE": "mock", "ASR_PROVIDER": "mock", "LLM_PROVIDER": "heuristic",
        "ASR_MOCK_TEXT": "缓存键应包含模型版本，数据库事务保证额度只结算一次。",
        "MAX_FILE_SIZE_MB": "32", "STORAGE_LOCAL_PATH": str(tmp_path / "storage"),
        "REDIS_URL": redis_url, "CELERY_BROKER_URL": "redis+socket://" + unix_path + "?virtual_host=0",
        "CELERY_RESULT_BACKEND": "cache+memory://", "UPLOAD_TEST_SCHEMA": schema_name,
        "UPLOAD_TEST_QUEUE": "uploads-smoke-" + schema_name,
        "JWT_SECRET_KEY": settings.JWT_SECRET_KEY}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    processes, logs = [], []
    try:
        for mode in ("worker", "api"):
            log = (tmp_path / f"{mode}.log").open("w")
            logs.append(log)
            process = subprocess.Popen([sys.executable, str(wrapper)], env={**environment,
                "UPLOAD_TEST_MODE": mode, "UPLOAD_TEST_FD": str(listener.fileno())},
                stdout=log, stderr=log, start_new_session=True, cwd=backend_dir,
                pass_fds=(listener.fileno(),) if mode == "api" else ())
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
