"""Stage A: genuine auth, pure administrative reads and atomic first-admin setup."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select

from test_tasks import repository, MemoryBus
from app.core.config import settings
from app.core.exceptions import AppException
from app.main import create_app
from app.modules.admin.router import get_admin_service
from app.modules.admin.service import AdminService
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import AuditRepository
from app.modules.auth.dependencies import get_auth_service
from app.modules.auth.models import User
from app.modules.auth.repository import UserRepository
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import issue_token
from app.modules.billing.models import Allowance, AllowanceAccount, AllowanceEvent, DailyAllowance
from app.modules.billing.policy import now
from app.modules.billing.repository import AllowanceRepository
from app.modules.tasks.router import get_task_service
from app.modules.tasks.service import TaskService


@pytest.fixture
def admin_data(repository, monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "admin-tests-isolated-secret-at-least-32-chars")
    users = UserRepository(repository.sessions)
    operator = users.subscription("private-admin-openid", True, login=True)
    target = users.subscription("private-target-openid", True, login=True)
    unbound = users.subscription("private-unbound-openid", True)
    service = AdminService(repository.sessions)
    app = create_app()
    app.dependency_overrides[get_admin_service] = lambda: service
    app.dependency_overrides[get_auth_service] = lambda: AuthService(users, None)
    app.dependency_overrides[get_task_service] = lambda: TaskService(repository, MemoryBus())
    return SimpleNamespace(sessions=repository.sessions, users=users, operator=operator,
        target=target, unbound=unbound, service=service, app=app, tasks=repository)


def bootstrap(data, key="initial-admin"):
    return data.service.bootstrap(data.operator.id, "local-operator", "初始化项目管理员", key)


def headers(data):
    return {"Authorization": "Bearer " + issue_token(data.operator.id)}


def test_bootstrap_is_atomic_idempotent_and_has_minimal_audit(admin_data):
    d = admin_data
    first = bootstrap(d)
    assert first["duplicate"] is False
    assert bootstrap(d) == {**first, "duplicate": True}
    assert d.users.get(d.operator.id).role == "admin"
    with d.sessions() as session:
        audit = session.scalar(select(AuditEvent))
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert audit.target_id == str(d.operator.id)
        assert audit.actor_kind == "operator" and audit.actor_user_id is None
        assert audit.actor_label == "local-operator" and audit.reason == "初始化项目管理员"
        assert audit.request_id.startswith("cli_") and audit.outcome == "succeeded"
        assert audit.changes == {"role": {"before": "user", "after": "admin"}}
        assert "openid" not in str(audit.changes)
    for changes in [(d.target.id, "local-operator", "初始化项目管理员"),
                    (d.operator.id, "different-actor", "初始化项目管理员"),
                    (d.operator.id, "local-operator", "different-reason")]:
        with pytest.raises(AppException) as error:
            d.service.bootstrap(*changes, "initial-admin")
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AppException) as error:
        d.service.bootstrap(d.target.id, "local-operator", "another admin", "another-key")
    assert error.value.code == "ADMIN_ALREADY_INITIALIZED"
    assert d.users.get(d.target.id).role == "user"


def test_bootstrap_rolls_back_role_and_audit_on_failure(admin_data):
    d = admin_data

    class BrokenAudit(AuditRepository):
        def record_bootstrap(self, *args):
            super().record_bootstrap(*args)
            raise RuntimeError("injected audit failure after flush")

    with pytest.raises(RuntimeError):
        AdminService(d.sessions, BrokenAudit()).bootstrap(d.operator.id, "operator", "reason", "rollback")
    assert d.users.get(d.operator.id).role == "user"
    with d.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_bootstrap_rejects_missing_unbound_disabled_and_invalid_input(admin_data):
    d = admin_data
    for identifier, expected in [(uuid4(), "USER_NOT_FOUND"), (d.unbound.id, "ADMIN_USER_INELIGIBLE")]:
        with pytest.raises(AppException) as error:
            d.service.bootstrap(identifier, "operator", "reason", "key")
        assert error.value.code == expected
    with d.sessions.begin() as session:
        session.get(User, d.operator.id).status = "disabled"
    with pytest.raises(AppException) as error:
        bootstrap(d)
    assert error.value.code == "ADMIN_USER_INELIGIBLE"
    for actor, reason, key in [(" ", "reason", "key"), ("op", "\n", "key"),
                               ("op", "reason", "k" * 129), ("op", "r" * 501, "key"),
                               ("op", "reason", "cleanup-result:fixture")]:
        with pytest.raises(AppException) as error:
            d.service.bootstrap(d.target.id, actor, reason, key)
        assert error.value.code == "INVALID_ADMIN_OPERATION"
    with d.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


async def test_every_admin_route_requires_actual_active_admin(admin_data):
    d = admin_data
    paths = ["/users", f"/users/{d.target.id}", f"/users/{d.target.id}/allowances", f"/users/{d.target.id}/ledger"]
    token = issue_token(d.operator.id)  # Role is read from DB, not embedded in this token.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test") as client:
        for path in paths:
            response = await client.get("/api/v1/admin" + path)
            assert response.status_code == 401 and response.json()["error"]["request_id"].startswith("req_")
            assert (await client.get("/api/v1/admin" + path, headers=headers(d))).status_code == 403
            assert (await client.get("/api/v1/admin" + path, params={"token": token})).status_code == 401
        bootstrap(d)
        for path in paths:
            response = await client.get("/api/v1/admin" + path, headers=headers(d))
            assert response.status_code == 200
            assert "wechat_openid" not in response.text and "wechat_unionid" not in response.text
            assert "private-" not in response.text
        # Admin rights never become an owner bypass for private task/result APIs.
        foreign = d.tasks.create("simulate_progress", user_id=d.target.id)
        assert (await client.get(f"/api/v1/tasks/{foreign.id}", headers=headers(d))).status_code == 403
        with d.sessions.begin() as session:
            session.get(User, d.operator.id).role = "user"
        assert (await client.get("/api/v1/admin/users", headers=headers(d))).status_code == 403
        with d.sessions.begin() as session:
            user = session.get(User, d.operator.id)
            user.role, user.status = "admin", "disabled"
        for path in paths:
            assert (await client.get("/api/v1/admin" + path, headers=headers(d))).status_code == 401


async def test_user_filters_pagination_and_validation(admin_data):
    d = admin_data
    bootstrap(d)
    quotas = AllowanceRepository(d.sessions)
    quotas.grant(d.target.id, "monthly", "fixture-member", "fixture")
    with d.sessions.begin() as session:
        session.get(User, d.target.id).nickname = "Alice_100%"
        user = session.get(User, d.unbound.id)
        user.nickname, user.status, user.is_subscribed = "alice-other", "disabled", False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test", headers=headers(d)) as client:
        base = "/api/v1/admin/users"
        all_users = (await client.get(base)).json()["data"]
        page = (await client.get(base, params={"limit": 1, "offset": 1})).json()["data"]
        assert page["total"] == 3 and page["items"] == all_users["items"][1:2]
        for query, expected in [({"q": "ALICE"}, 2), ({"q": "%"}, 1), ({"q": "_"}, 2),
            ({"q": str(d.target.id)}, 1), ({"member": "true"}, 1), ({"member": "false"}, 2),
            ({"status": "disabled", "subscribed": "false"}, 1), ({"offset": 100}, 0)]:
            data = (await client.get(base, params=query)).json()["data"]
            assert len(data["items"]) == expected
        for query in [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"q": "a" * 101},
                      {"status": "deleted"}, {"member": "maybe"}]:
            assert (await client.get(base, params=query)).status_code == 422
        for suffix in ["", "/allowances", "/ledger"]:
            assert (await client.get(f"{base}/{uuid4()}{suffix}")).status_code == 404
        assert (await client.get(base + "/invalid-uuid")).status_code == 422
        # No HTTP initialization or unapproved write endpoints.
        assert (await client.post("/api/v1/admin/bootstrap", json={})).status_code == 404
        assert (await client.post(f"{base}/{d.target.id}/grants", json={})).status_code == 422


def test_admin_reads_never_initialize_or_expire_billing_data(admin_data):
    d = admin_data
    quotas = AllowanceRepository(d.sessions)
    quotas.balance(d.target.id)
    with d.sessions.begin() as session:
        session.get(User, d.target.id).status = "disabled"
        for lot in session.scalars(select(Allowance)):
            lot.expires_at = now() - timedelta(seconds=1)
        # No expire event has been generated; reads must leave it that way.
    statements = []
    engine = d.sessions.kw["bind"]

    def track(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", track)
    try:
        for user in [d.target, d.unbound]:
            d.service.user_detail(user.id)
            result = d.service.user_allowances(user.id)
            assert result["balances"]["asr_seconds"]["available"] == 0
            assert result["balances"]["ai_generations"]["usable"] == 0
            d.service.user_ledger(user.id)
        d.service.list_users()
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert set(statements) == {"SELECT"}
    with d.sessions() as session:
        assert session.get(AllowanceAccount, d.unbound.id) is None
        assert session.get(DailyAllowance, (d.unbound.id, now().date())) is None
        assert session.scalar(select(func.count()).select_from(AllowanceEvent).where(AllowanceEvent.event == "expire")) == 0


def test_allowance_snapshot_pending_expired_held_and_disabled_semantics(admin_data):
    d = admin_data
    quotas = AllowanceRepository(d.sessions)
    quotas.grant(d.target.id, "monthly", "first-period", "fixture")
    quotas.grant(d.target.id, "monthly", "next-period", "fixture")
    result = d.service.user_allowances(d.target.id, limit=1)
    assert result["lots"]["total"] == 6 and len(result["lots"]["items"]) == 1
    assert result["balances"]["asr_seconds"] == {"available": 19800, "held": 0, "usable": 19800}
    assert result["balances"]["ai_generations"]["available"] == 105
    with d.sessions.begin() as session:
        user = session.get(User, d.target.id)
        user.status, user.is_subscribed = "disabled", False
        trial = session.scalar(select(Allowance).where(Allowance.kind == "asr_seconds", Allowance.source == "trial"))
        trial.held, trial.expires_at = 60, now() - timedelta(seconds=1)
    result = d.service.user_allowances(d.target.id)
    assert result["balances"]["asr_seconds"] == {"available": 18000, "held": 60, "usable": 0}
    assert result["balances"]["ai_generations"] == {"available": 100, "held": 0, "usable": 0}
    assert all(lot["usable"] == 0 for lot in result["lots"]["items"])
    assert any(lot["future"] for lot in result["lots"]["items"])
    ledger = d.service.user_ledger(d.target.id, limit=1, offset=1)
    assert len(ledger["items"]) == 1 and ledger["total"] == 3


def test_cli_reports_idempotence_and_sanitizes_unexpected_errors(admin_data, monkeypatch, capsys):
    from app.modules.admin import manage
    monkeypatch.setattr(manage, "SessionLocal", admin_data.sessions)
    args = ["bootstrap", "--user-id", str(admin_data.operator.id), "--actor", "operator",
            "--reason", "initial setup", "--key", "cli-test"]
    assert manage.main(args) == 0
    assert '"duplicate": false' in capsys.readouterr().out
    assert manage.main(args) == 0
    assert '"duplicate": true' in capsys.readouterr().out

    def fail(*args):
        raise RuntimeError("postgresql://secret:password@internal")

    monkeypatch.setattr(AdminService, "bootstrap", fail)
    assert manage.main(args) == 1
    output = capsys.readouterr().out
    assert "ADMIN_OPERATION_FAILED" in output and "password" not in output
