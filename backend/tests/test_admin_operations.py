from datetime import timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from test_admin import admin_data, bootstrap, headers
from test_tasks import repository
from app.core.exceptions import AppException
from app.modules.admin.service import AdminService
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import AuditRepository
from app.modules.billing.policy import BEIJING, now
from app.modules.system.service import StorageStatusService, get_cleanup_service, get_storage_status_service
from app.modules.tasks.models import Task


COUNTS = {"files": 1, "temporary_directories": 0, "orphan_files": 0, "freed_bytes": 1024, "used_bytes": 0}


async def test_all_new_admin_routes_reject_anonymous_and_ordinary_users(admin_data):
    d = admin_data
    task = d.tasks.create("download", user_id=d.target.id)
    reads = ["/tasks", f"/tasks/{task.id}", "/audit-events", "/overview", "/storage",
             f"/users/{d.target.id}/grants/preview?product=monthly"]
    writes = [(f"/tasks/{task.id}/cancel", {"reason": "fixture", "key": "cancel"}),
              ("/storage/cleanup", {"reason": "fixture", "key": "cleanup"}),
              (f"/users/{d.target.id}/grants", {"reason": "fixture", "key": "grant", "product": "ai_100"}),
              (f"/users/{d.target.id}/status", {"reason": "fixture", "key": "status", "status": "disabled",
                  "expected_status": "active", "expected_version": 0})]
    cleaner = Mock()
    d.app.dependency_overrides[get_cleanup_service] = lambda: cleaner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test") as client:
        for path in reads:
            assert (await client.get("/api/v1/admin" + path)).status_code == 401
            assert (await client.get("/api/v1/admin" + path, headers=headers(d))).status_code == 403
        for path, body in writes:
            assert (await client.post("/api/v1/admin" + path, json=body)).status_code == 401
            assert (await client.post("/api/v1/admin" + path, json=body, headers=headers(d))).status_code == 403
    cleaner.run.assert_not_called()
    assert d.tasks.get(task.id).status == "queued"


async def test_audit_filters_failed_attempts_and_beijing_overview(admin_data):
    d = admin_data
    bootstrap(d)
    actor = d.users.get(d.operator.id)
    d.service.change_user_status(actor, d.target.id, "disabled", "active", 0, "fixture", "disable", "req_change")
    with pytest.raises(AppException):
        d.service.change_user_status(actor, d.target.id, "active", "disabled", 0, "stale", "conflict", "req_conflict")
    today = now().astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    old = d.tasks.create("download", user_id=d.target.id)
    d.tasks.create("download", user_id=d.target.id)
    d.tasks.delete_history(d.target.id, old.id)
    with d.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == old.id).values(created_at=(today - timedelta(seconds=1)).astimezone(timezone.utc)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test", headers=headers(d)) as client:
        query = {"actor_id": str(actor.id), "action": "user.status", "outcome": "failed",
                 "target_type": "user", "target_id": str(d.target.id)}
        response = await client.get("/api/v1/admin/audit-events", params=query)
        page = response.json()["data"]
        assert page["total"] == 1 and page["items"][0]["request_id"] == "req_conflict"
        assert page["items"][0]["result"]["error_code"] == "USER_STATUS_CONFLICT"
        assert page["items"][0]["request_data"]["operation_key"] == "conflict"
        page = (await client.get("/api/v1/admin/audit-events", params={"limit": 1, "offset": 1})).json()["data"]
        assert page["total"] == 3 and len(page["items"]) == 1
        for params in [{"since": "2026-10-26T00:00:00"}, {"limit": 101}, {"outcome": "deleted"},
                       {"since": now().isoformat(), "until": (now() - timedelta(days=1)).isoformat()}]:
            assert (await client.get("/api/v1/admin/audit-events", params=params)).status_code == 422
        overview = (await client.get("/api/v1/admin/overview")).json()["data"]
        assert overview["users"] == {"total": 3, "active": 2, "disabled": 1, "subscribed": 3}
        assert overview["tasks"]["total"] == 2 and overview["tasks"]["created_today"] == 1
        today_tasks = (await client.get("/api/v1/admin/tasks", params={"since": today.isoformat()})).json()["data"]
        assert today_tasks["total"] == 1
        assert overview["revenue"] is None and overview["supplier_cost"] is None
        assert overview["timezone"] == "Asia/Shanghai" and not overview["real_payment_enabled"]
        assert overview["media"]["registered_files"] == 0
        assert overview["worker"]["liveness"] == "not_measured"
        assert (await client.delete("/api/v1/admin/audit-events")).status_code == 405


async def test_cleanup_intent_and_completion_are_audited_once_including_legacy_route(admin_data):
    d = admin_data
    bootstrap(d)
    cleaner = Mock(run=Mock(return_value={**COUNTS, "internal_path": "PRIVATE_PATH"}))
    d.app.dependency_overrides[get_cleanup_service] = lambda: cleaner
    body = {"reason": "清理过期文件", "key": "cleanup-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test", headers=headers(d)) as client:
        response = await client.post("/api/v1/admin/storage/cleanup", json=body)
        first = response.json()["data"]
        assert response.status_code == 200 and first["state"] == "completed" and first["freed_bytes"] == 1024
        assert "PRIVATE_PATH" not in response.text
        repeated = (await client.post("/api/v1/admin/storage/cleanup", json=body)).json()["data"]
        assert repeated == {**first, "duplicate": True}
        cleaner.run.assert_called_once()
        # Existing settings client remains compatible and does not bypass audit.
        legacy = await client.post("/api/v1/system/cleanup")
        assert legacy.status_code == 200 and legacy.json()["data"]["freed_bytes"] == 1024
        audit = (await client.get("/api/v1/admin/audit-events", params={"target_type": "storage"})).json()["data"]
        assert audit["total"] == 4
        assert {row["action"] for row in audit["items"]} == {"storage.cleanup.request", "storage.cleanup.finish"}


async def test_cleanup_failure_and_unknown_outcome_are_not_reexecuted(admin_data):
    d = admin_data
    bootstrap(d)
    actor = d.users.get(d.operator.id)
    cleaner = Mock(run=Mock(side_effect=AppException("STORAGE_BUSY", "fixture", 503)))
    with pytest.raises(AppException):
        await d.service.cleanup(actor, "fixture", "busy", "req_busy", cleaner)
    duplicate = await d.service.cleanup(actor, "fixture", "busy", "req_retry", cleaner)
    assert duplicate["state"] == "failed_or_partial" and duplicate["error_code"] == "STORAGE_BUSY"
    cleaner.run.assert_called_once()
    # Simulate an API crash after intent commit; the same key must not blindly run again.
    d.service.begin_cleanup(actor, "fixture", "crash", "req_crash")
    unknown = await d.service.cleanup(actor, "fixture", "crash", "req_retry", cleaner)
    assert unknown["state"] == "accepted" and unknown["requires_review"]
    cleaner.run.assert_called_once()


async def test_cleanup_finish_audit_failure_reports_unknown_after_filesystem_change(admin_data):
    d = admin_data
    bootstrap(d)

    class FinishFailure(AuditRepository):
        def record_action(self, *args, **kwargs):
            if kwargs["action"] == "storage.cleanup.finish":
                raise AppException("DATABASE_UNAVAILABLE", "fixture", 503)
            return super().record_action(*args, **kwargs)

    service = AdminService(d.sessions, FinishFailure())
    cleaner = Mock(run=Mock(return_value=COUNTS))
    result = await service.cleanup(d.users.get(d.operator.id), "fixture", "unknown", "req_1", cleaner)
    assert result["state"] == "outcome_unrecorded" and result["requires_review"]
    assert result["result"]["freed_bytes"] == 1024
    repeated = await service.cleanup(d.users.get(d.operator.id), "fixture", "unknown", "req_2", cleaner)
    assert repeated["requires_review"]
    cleaner.run.assert_called_once()


def test_storage_scan_counts_temporary_files_and_marks_partial_unknown(tmp_path):
    root = tmp_path / "storage"
    (root / "tmp").mkdir(parents=True)
    (root / "files").mkdir()
    (root / "tmp" / "partial").write_bytes(b"x" * 12)
    (root / "files" / "video").write_bytes(b"x" * 20)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"x" * 1000)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    metrics = StorageStatusService(root).snapshot()
    assert metrics["used_bytes"] == 32 and metrics["scan_complete"]
    assert str(root) not in str(metrics) and metrics["quota_bytes"] > 0
    partial = StorageStatusService(root, max_entries=1).snapshot()
    assert partial["status"] == "partial" and partial["used_bytes"] is None
    missing = tmp_path / "missing"
    StorageStatusService(missing).snapshot()
    assert not missing.exists()
