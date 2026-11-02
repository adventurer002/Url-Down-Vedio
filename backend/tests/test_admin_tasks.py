from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from test_admin import admin_data, bootstrap, headers
from test_tasks import repository, MemoryBus
from test_allowances import billing
from test_download import storage, video
from app.core.exceptions import AppException
from app.modules.admin.service import AdminService
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import AuditRepository
from app.modules.auth.repository import UserRepository
from app.modules.billing.policy import now
from app.modules.tasks.router import get_task_service
from app.modules.tasks.service import TaskService


async def test_global_queries_include_hidden_records_and_exclude_private_payloads(admin_data):
    d = admin_data
    bootstrap(d)
    private = d.tasks.create("ai_question", user_id=d.target.id,
        input_payload={"question": "PRIVATE_QUESTION", "url": "https://private.example/?token=SECRET"})
    answer_id = uuid4()
    d.tasks.start(private.id)
    d.tasks.finish(private.id, "completed", 100,
        {"answer_id": str(answer_id), "private_answer": "PRIVATE_ANSWER", "file_id": "not-a-uuid"},
        error_message="PRIVATE_ERROR")
    d.tasks.hide_history(d.target.id, private.id)
    queued = d.tasks.create("download", user_id=d.target.id)
    anonymous = d.tasks.create("transcription")
    bus = MemoryBus()
    await bus.publish(queued.id, "queued", 0, stage="queued")
    tasks = TaskService(d.tasks, bus)
    d.app.dependency_overrides[get_task_service] = lambda: tasks
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test", headers=headers(d)) as client:
        response = await client.get("/api/v1/admin/tasks")
        assert response.status_code == 200
        page = response.json()["data"]
        assert page["total"] == 3
        assert "PRIVATE" not in response.text and "SECRET" not in response.text
        for forbidden in ["input_payload", "result_payload", "worker_pid", "worker_host", "error_message"]:
            assert forbidden not in response.text
        detail = (await client.get(f"/api/v1/admin/tasks/{private.id}")).json()["data"]
        assert detail["history_hidden"] and detail["result_references"] == {"answer_id": str(answer_id)}
        assert detail["status"] == "completed" and detail["queue_seconds"] >= 0 and detail["execution_seconds"] >= 0
        query = {"user_id": str(d.target.id), "status": "queued", "task_type": "download",
                 "since": (now() - timedelta(minutes=1)).isoformat(), "until": (now() + timedelta(minutes=1)).isoformat()}
        selected = (await client.get("/api/v1/admin/tasks", params=query)).json()["data"]
        assert selected["total"] == 1 and selected["items"][0]["task_id"] == str(queued.id)
        paged = (await client.get("/api/v1/admin/tasks", params={"limit": 1, "offset": 1})).json()["data"]
        assert len(paged["items"]) == 1 and paged["total"] == 3
        # A stale progress snapshot cannot overwrite a committed completion.
        await bus.publish(private.id, "running", 50)
        assert (await client.get(f"/api/v1/admin/tasks/{private.id}")).json()["data"]["status"] == "completed"
        bus.snapshot = AsyncMock(side_effect=AppException("REDIS_UNAVAILABLE", "fixture", 503))
        assert (await client.get(f"/api/v1/admin/tasks/{anonymous.id}")).status_code == 200
        for params in [{"status": "bogus"}, {"task_type": "shell"}, {"since": "2026-10-26T00:00:00"},
                       {"since": now().isoformat(), "until": (now() - timedelta(days=1)).isoformat()}, {"limit": 101}]:
            assert (await client.get("/api/v1/admin/tasks", params=params)).status_code == 422


async def test_admin_cancel_is_audited_cooperative_and_handles_redis_outage(admin_data):
    d = admin_data
    bootstrap(d)
    task = d.tasks.create("transcription", user_id=d.target.id)
    d.tasks.start(task.id)
    bus = MemoryBus()

    async def unavailable(task_id, persist):
        await persist()
        raise AppException("REDIS_UNAVAILABLE", "fixture", 503)

    bus.request_task_cancel = unavailable
    d.app.dependency_overrides[get_task_service] = lambda: TaskService(d.tasks, bus)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test", headers=headers(d)) as client:
        path = f"/api/v1/admin/tasks/{task.id}/cancel"
        body = {"reason": "用户请求协助取消", "key": "admin-cancel"}
        response = await client.post(path, json=body)
        result = response.json()["data"]
        assert response.status_code == 200 and result["status"] == "running" and result["cancel_requested"]
        assert result["notification_pending"] and d.tasks.get(task.id).status == "running"
        assert (await client.post(path, json=body)).json()["data"]["duplicate"]
        d.tasks.finish(task.id, "cancelled", 0)
        audit = (await client.get("/api/v1/admin/audit-events", params={"action": "task.cancel"})).json()["data"]
        assert audit["total"] == 1 and audit["items"][0]["outcome"] == "accepted"
        assert audit["items"][0]["current_task_status"] == "cancelled"


def test_queued_cancel_and_late_cancel_preserve_completed_result(admin_data):
    d = admin_data
    bootstrap(d)
    actor = d.users.get(d.operator.id)
    queued = d.tasks.create("download", user_id=d.target.id)
    assert d.service.cancel_task(actor, queued.id, "fixture", "queued-cancel", "req_1")["status"] == "cancelled"
    completed = d.tasks.create("download", user_id=d.target.id)
    payload = {"file_id": str(uuid4())}
    d.tasks.finish(completed.id, "completed", 100, payload)
    result = d.service.cancel_task(actor, completed.id, "fixture", "late-cancel", "req_2")
    assert result["status"] == "completed" and not result["cancel_requested"]
    assert d.tasks.get(completed.id).result_payload == payload
    with d.sessions() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.idempotency_key == "late-cancel")).outcome == "unchanged"


def test_cancel_rollback_does_not_commit_intent_without_audit(admin_data):
    d = admin_data
    bootstrap(d)
    task = d.tasks.create("download", user_id=d.target.id)

    class BrokenAudit(AuditRepository):
        def record_action(self, *args, **kwargs):
            raise RuntimeError("fixture")

    with pytest.raises(RuntimeError):
        AdminService(d.sessions, BrokenAudit()).cancel_task(d.users.get(d.operator.id), task.id,
            "fixture", "rollback", "req_1")
    stored = d.tasks.get(task.id)
    assert stored.status == "queued" and not stored.cancel_requested


def test_admin_cancel_releases_allowance_only_on_final_worker_outcome(billing):
    b = billing
    users = UserRepository(b.tasks.sessions)
    admin = users.subscription("task-admin", True, login=True)
    service = AdminService(b.tasks.sessions)
    service.bootstrap(admin.id, "operator", "fixture", "bootstrap")
    task = b.create()
    assert b.tasks.start(task.id)
    result = service.cancel_task(users.get(admin.id), task.id, "fixture", "cancel", "req_1")
    assert result["status"] == "running" and result["cancel_requested"]
    assert service.task_detail(task.id)["allowance"]["state"] == "held"
    b.tasks.finish(task.id, "cancelled", 0)
    usage = service.task_detail(task.id)["allowance"]
    assert usage["state"] == "released" and usage["settlement"]["ai_generations"] == 0
    service.cancel_task(users.get(admin.id), task.id, "fixture", "cancel-again", "req_2")
    assert b.quotas.balance(b.user.id)["balances"]["ai_generations"]["available"] == 5
