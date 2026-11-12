"""Disposable live HTTP smoke check; all identity, storage and billing data are local fixtures."""
import asyncio
import json
import os
from uuid import uuid4
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn
from app.core.config import settings
from app.infrastructure.database.base import Base
from app.infrastructure.storage.local import LocalStorageAdapter
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.main import create_app
from app.modules.admin.router import get_admin_service
from app.modules.admin.service import AdminService
from app.modules.auth.dependencies import get_auth_service
from app.modules.auth.repository import UserRepository
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import issue_token
from app.modules.media.repository import MediaRepository
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.router import get_task_service
from app.modules.tasks.service import TaskService
from app.modules.system.service import StorageStatusService, get_cleanup_service, get_storage_status_service
from app.modules.billing import service as billing_service


class Bus:
    async def snapshot(self, task_id):
        return None

    async def request_task_cancel(self, task_id, persist):
        return await persist()


async def offline_dispatch(*args, **kwargs):
    return 0


def main():
    settings.APP_ENV = "development"
    settings.AUTH_REQUIRED = False
    settings.ALLOWANCES_ENABLED = False
    settings.JWT_SECRET_KEY = "disposable-smoke-fixture-secret-at-least-32-chars"
    billing_service.resume_queued = offline_dispatch
    
    with tempfile.TemporaryDirectory(prefix="videohub-admin-smoke-") as temporary:
        root = Path(temporary)
        engine = create_engine("sqlite:///" + str(root / "fixture.db"), connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        users, tasks = UserRepository(sessions), TaskRepository(sessions)
        admin = users.subscription("smoke-admin", True, login=True)
        target = users.subscription("smoke-user", True, login=True)
        service = AdminService(sessions)
        service.bootstrap(admin.id, "smoke-operator", "isolated HTTP smoke", "smoke-bootstrap")
        storage = LocalStorageAdapter(root / "storage")
        expired = storage.root / "files" / (str(uuid4()) + ".mp4")
        fresh = storage.root / "files" / (str(uuid4()) + ".mp4")
        expired.write_bytes(b"expired disposable fixture" * 1024)
        fresh.write_bytes(b"fresh disposable fixture")
        old = time.time() - 72 * 3600
        os.utime(expired, (old, old))
    
        class Cleaner:
            def run(self):
                return asyncio.run(cleanup_expired_files(storage, MediaRepository(sessions), tasks))
    
        app = create_app()
        app.dependency_overrides[get_admin_service] = lambda: service
        app.dependency_overrides[get_auth_service] = lambda: AuthService(users, None)
        app.dependency_overrides[get_task_service] = lambda: TaskService(tasks, Bus())
        app.dependency_overrides[get_cleanup_service] = lambda: Cleaner()
        app.dependency_overrides[get_storage_status_service] = lambda: StorageStatusService(storage.root)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        checks = []
        admin_token, target_token = issue_token(admin.id), issue_token(target.id)
    
        def request(path, expected=200, token=admin_token, body=None):
            arguments = ["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "8",
                         "--write-out", "\n%{http_code}", f"http://127.0.0.1:{port}" + path]
            if token:
                arguments += ["-H", "Authorization: Bearer " + token]
            if body is not None:
                arguments += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
            response = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=10).stdout
            content, status = response.rsplit("\n", 1)
            assert int(status) == expected, (path, status)
            checks.append({"path": path, "status": int(status)})
            return json.loads(content)
    
        try:
            request("/api/v1/admin/users", 401, token=None)
            request("/api/v1/admin/users", 403, token=target_token)
            request("/api/v1/admin/users")
            request(f"/api/v1/admin/users/{target.id}")
            request(f"/api/v1/admin/users/{target.id}/allowances")
            request(f"/api/v1/admin/users/{target.id}/grants/preview?product=monthly")
            grant = {"product": "monthly", "reason": "isolated fixture grant", "key": "smoke-grant"}
            request(f"/api/v1/admin/users/{target.id}/grants", body=grant)
            assert request(f"/api/v1/admin/users/{target.id}/grants", body=grant)["data"]["duplicate"]
            request(f"/api/v1/admin/users/{target.id}/ledger")
            request(f"/api/v1/admin/users/{target.id}/status", body={"status": "disabled", "expected_status": "active",
                "expected_version": 0, "reason": "fixture disable", "key": "smoke-disable"})
            request("/api/v1/auth/me", 401, token=target_token)
            request(f"/api/v1/admin/users/{target.id}/status", body={"status": "active", "expected_status": "disabled",
                "expected_version": 1, "reason": "fixture restore", "key": "smoke-restore"})
            request("/api/v1/auth/me", 401, token=target_token)
            request("/api/v1/auth/me", token=issue_token(target.id, 2))
            task = tasks.create("simulate_progress", user_id=target.id, input_payload={"question": "PRIVATE_FIXTURE"})
            tasks.delete_history(target.id, task.id)
            assert "PRIVATE_FIXTURE" not in json.dumps(request("/api/v1/admin/tasks"))
            request(f"/api/v1/admin/tasks/{task.id}")
            assert request(f"/api/v1/admin/tasks/{task.id}/cancel", body={"reason": "fixture cancel", "key": "smoke-cancel"})["data"]["status"] == "cancelled"
            request("/api/v1/admin/audit-events")
            assert request("/api/v1/admin/overview")["data"]["revenue"] is None
            request("/api/v1/admin/storage")
            cleanup = {"reason": "fixture cleanup", "key": "smoke-cleanup"}
            result = request("/api/v1/admin/storage/cleanup", body=cleanup)["data"]
            assert result["state"] == "completed" and result["orphan_files"] == 1
            assert not expired.exists() and fresh.exists()
            assert result["freed_bytes"] > 0
            assert request("/api/v1/admin/storage/cleanup", body=cleanup)["data"]["duplicate"]
            audit = request("/api/v1/admin/audit-events?target_type=storage&target_id=local")["data"]
            assert audit["total"] == 2
            assert {row["action"] for row in audit["items"]} == {"storage.cleanup.request", "storage.cleanup.finish"}
            assert request("/api/v1/admin/audit-events?limit=1&offset=1")["data"]["limit"] == 1
            request("/api/v1/admin/audit-events?actor_id=bad", expected=422)
            request("/api/v1/admin/audit-events?since=2026-10-26T00:00:00", expected=422)
            request("/api/v1/admin/audit-events?outcome=deleted", expected=422)
            request("/api/v1/admin/storage/cleanup", expected=401, token=None, body=cleanup)
            request("/api/v1/admin/storage/cleanup", expected=403, token=issue_token(target.id, 2), body=cleanup)
            request("/api/v1/admin/storage/cleanup", expected=422, body={"reason": " ", "key": "invalid"})
            spec = request("/openapi.json", token=None)
            assert "BearerAuth" in spec["components"]["securitySchemes"]
            print(json.dumps({"checks_passed": len(checks), "status_codes": sorted({x["status"] for x in checks}),
                              "database": "disposable SQLite", "storage": "disposable directory",
                              "external_model_calls": 0}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            engine.dispose()
            assert not thread.is_alive()


if __name__ == "__main__":
    main()
