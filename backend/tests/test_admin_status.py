import httpx
import pytest
from sqlalchemy import func, select

from test_admin import admin_data, bootstrap, headers
from test_wechat_auth import auth, login
from test_allowances import billing
from test_tasks import repository, redis_url
from test_download import storage, video
from app.core.exceptions import AppException
from app.modules.admin.service import AdminService
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import AuditRepository
from app.modules.auth.repository import UserRepository
from app.modules.auth.tokens import issue_token


def change(d, status="disabled", expected="active", version=0, key="disable-1"):
    actor = d.users.get(d.operator.id)
    return d.service.change_user_status(actor, d.target.id, status, expected, version, "账号风控处理", key, "req_test")


async def test_status_mutation_invalidates_old_credentials_after_restore(admin_data):
    d = admin_data
    bootstrap(d)
    token = issue_token(d.target.id)
    body = {"status": "disabled", "expected_status": "active", "expected_version": 0,
            "reason": "账号风控处理", "key": "disable-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test") as client:
        path = f"/api/v1/admin/users/{d.target.id}/status"
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.post(path, json=body, headers={"Authorization": "Bearer " + token})).status_code == 403
        client.cookies.set("access_token", issue_token(d.operator.id))
        assert (await client.post(path, json=body)).json()["error"]["code"] == "ADMIN_BEARER_REQUIRED"
        response = await client.post(path, json=body, headers=headers(d))
        result = response.json()["data"]
        assert response.status_code == 200 and result["status_version"] == 1
        assert (await client.post(path, json=body, headers=headers(d))).json()["data"] == {**result, "duplicate": True}
        assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 401
        restored = change(d, "active", "disabled", 1, "restore-1")
        assert restored["status_version"] == 2
        assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 401
        fresh = issue_token(d.target.id, 2)
        assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + fresh})).status_code == 200
        # The original disable retry replays history, it cannot disable the restored user again.
        assert (await client.post(path, json=body, headers=headers(d))).json()["data"]["duplicate"]
        assert d.users.get(d.target.id).status == "active"


def test_status_optimistic_conflict_admin_protection_and_noop(admin_data):
    d = admin_data
    bootstrap(d)
    result = change(d, "active")
    assert result["status_version"] == 0
    with d.sessions() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.idempotency_key == "disable-1")).outcome == "unchanged"
    change(d, key="real-disable")
    with pytest.raises(AppException) as error:
        change(d, key="stale-disable")
    assert error.value.code == "USER_STATUS_CONFLICT"
    with pytest.raises(AppException) as error:
        d.service.change_user_status(d.users.get(d.operator.id), d.operator.id, "disabled", "active", 0,
                                     "no self-disable", "self", "req_self")
    assert error.value.code == "ADMIN_ACCOUNT_PROTECTED"
    with pytest.raises(AppException) as error:
        change(d, "active", key="real-disable")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_status_audit_failure_rolls_back_token_invalidation(admin_data):
    d = admin_data
    bootstrap(d)

    class BrokenAudit(AuditRepository):
        def record_action(self, *args, **kwargs):
            super().record_action(*args, **kwargs)
            raise RuntimeError("audit failure")

    with pytest.raises(RuntimeError):
        AdminService(d.sessions, BrokenAudit()).change_user_status(d.users.get(d.operator.id), d.target.id,
            "disabled", "active", 0, "fixture", "rollback", "req_rollback")
    assert d.users.get(d.target.id).status == "active" and d.users.get(d.target.id).auth_version == 0
    with d.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


async def test_pending_wechat_redemption_and_live_sse_cannot_survive_disable_restore(auth):
    _, admin_login = await login(auth)
    _, target_login = await login(auth)
    from uuid import UUID
    admin_id, target_id = UUID(admin_login["user"]["id"]), UUID(target_login["user"]["id"])
    service = AdminService(auth.repository.sessions)
    service.bootstrap(admin_id, "operator", "fixture", "bootstrap")
    actor = await auth.service.user(admin_id)
    pending = await auth.service.create_session()
    with auth.repository.sessions() as session:
        from app.modules.auth.models import User
        openid = session.get(User, target_id).wechat_openid
    assert await auth.service.authenticate(openid, pending["verify_code"])
    task = auth.repository.create("simulate_progress", user_id=target_id)
    subscription, initial = await auth.tasks.open_events(task.id)
    stream = auth.tasks.events(task.id, subscription, initial, lambda: auth.service.user(target_id, 0))
    await anext(stream)
    service.change_user_status(actor, target_id, "disabled", "active", 0, "fixture", "disable", "req_1")
    service.change_user_status(actor, target_id, "active", "disabled", 1, "fixture", "restore", "req_2")
    with pytest.raises(AppException) as error:
        await auth.service.poll(pending["scene_id"])
    assert error.value.code == "INVALID_TOKEN"
    with pytest.raises(AppException):
        await anext(stream)
    subscription.close.assert_awaited_once()
    # A new real login issues the current version, not zero.
    pending = await auth.service.create_session()
    assert await auth.service.authenticate(openid, pending["verify_code"])
    result = await auth.service.poll(pending["scene_id"])
    response = await auth.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + result["access_token"]})
    assert response.status_code == 200 and "auth_version" not in response.json()["data"]


def test_disabling_does_not_cancel_running_work_or_release_held_allowance(billing):
    b = billing
    users = UserRepository(b.tasks.sessions)
    admin = users.subscription("status-admin", True, login=True)
    service = AdminService(b.tasks.sessions)
    service.bootstrap(admin.id, "operator", "fixture", "bootstrap")
    running, queued = b.create(), b.create()
    assert b.tasks.start(running.id)
    actor = users.get(admin.id)
    service.change_user_status(actor, b.user.id, "disabled", "active", 0, "fixture", "disable", "req_1")
    assert b.tasks.get(running.id).status == "running" and not b.tasks.get(running.id).cancel_requested
    assert service.user_allowances(b.user.id)["balances"]["ai_generations"]["held"] == 1
    with pytest.raises(AppException):
        b.tasks.start(queued.id)
    b.tasks.finish(running.id, "failed", 0)
    assert service.user_allowances(b.user.id)["balances"]["ai_generations"]["held"] == 0
