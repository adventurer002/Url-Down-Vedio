from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from test_admin import admin_data, bootstrap, headers
from test_tasks import repository
from app.core.exceptions import AppException
from app.modules.admin.service import AdminService
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import AuditRepository
from app.modules.billing.models import AllowanceAccount, AllowanceGrant
from app.modules.billing.repository import AllowanceRepository
from app.modules.auth.models import User


def grant(d, product="monthly", key="manual-1", reason="人工权益发放"):
    return d.service.grant_user(d.users.get(d.operator.id), d.target.id, product, reason, key, "req_grant")


async def test_fixed_product_grant_api_identity_idempotency_and_queue_outage(admin_data, monkeypatch):
    d = admin_data
    bootstrap(d)
    from app.modules.billing import service as billing_service
    monkeypatch.setattr(billing_service, "resume_queued", AsyncMock(side_effect=AppException("QUEUE_UNAVAILABLE", "fixture", 503)))
    body = {"product": "monthly", "reason": "人工权益发放", "key": "manual-1"}
    path = f"/api/v1/admin/users/{d.target.id}/grants"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://test") as client:
        assert (await client.post(path, json=body)).status_code == 401
        client.cookies.set("access_token", headers(d)["Authorization"][7:])
        assert (await client.post(path, json=body)).json()["error"]["code"] == "ADMIN_BEARER_REQUIRED"
        for extra in [{"actor": "spoofed"}, {"amount": 999}, {"expires_at": "2099-01-01"}]:
            assert (await client.post(path, json={**body, **extra}, headers=headers(d))).status_code == 422
        response = await client.post(path, json=body, headers=headers(d))
        assert response.status_code == 200
        result = response.json()["data"]
        assert result["payment_verified"] is False and result["mode"] == "operator_grant"
        assert result["dispatch_pending"] and result["dispatched"] is None
        repeated = (await client.post(path, json=body, headers=headers(d))).json()["data"]
        assert repeated == {**result, "duplicate": True}
        assert (await client.post(path, json={**body, "reason": "different"}, headers=headers(d))).status_code == 409
        assert (await client.post(path, json={**body, "product": "ai_100"}, headers=headers(d))).status_code == 409
    with d.sessions() as session:
        record = session.get(AllowanceGrant, "manual-1")
        assert record.actor == str(d.operator.id)
        audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "allowance.grant"))
        assert audit.actor_user_id == d.operator.id and audit.request_id.startswith("req_")
        assert audit.request_data == {"product": "monthly"}
        assert session.scalar(select(func.count()).select_from(AllowanceGrant)) == 1


def test_grant_preview_is_pure_and_renewal_preserves_future_periods(admin_data):
    d = admin_data
    bootstrap(d)
    before = d.service.grant_preview(d.target.id, "monthly")
    assert before["member_until_before"] is None
    with d.sessions() as session:
        assert session.get(AllowanceAccount, d.target.id) is None
    first = grant(d)
    preview = d.service.grant_preview(d.target.id, "monthly")
    assert preview["starts_at"] == datetime.fromisoformat(first["expires_at"])
    second = grant(d, key="manual-2")
    assert second["starts_at"] == first["expires_at"]
    balance = d.service.user_allowances(d.target.id)
    assert balance["balances"]["ai_generations"]["available"] == 105
    assert any(item["future"] for item in balance["lots"]["items"])
    addon = grant(d, "asr_120", "asr-addon")
    assert (datetime.fromisoformat(addon["expires_at"]) - datetime.fromisoformat(addon["starts_at"])).days == 30
    assert d.service.user_allowances(d.target.id)["balances"]["asr_seconds"]["available"] == 27000


def test_grant_and_account_creation_roll_back_when_audit_fails(admin_data):
    d = admin_data
    bootstrap(d)

    class BrokenAudit(AuditRepository):
        def record_action(self, *args, **kwargs):
            super().record_action(*args, **kwargs)
            raise RuntimeError("audit failure")

    with pytest.raises(RuntimeError):
        AdminService(d.sessions, BrokenAudit()).grant_user(d.users.get(d.operator.id), d.target.id,
            "monthly", "fixture", "rollback", "req_rollback")
    with d.sessions() as session:
        assert session.get(AllowanceGrant, "rollback") is None
        assert session.get(AllowanceAccount, d.target.id) is None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_legacy_keys_and_disabled_users_cannot_be_reissued(admin_data):
    d = admin_data
    bootstrap(d)
    AllowanceRepository(d.sessions).grant(d.target.id, "ai_100", "legacy", "old-operator")
    with pytest.raises(AppException) as error:
        grant(d, "ai_100", "legacy")
    assert error.value.code == "LEGACY_GRANT_EXISTS"
    with d.sessions.begin() as session:
        session.get(User, d.target.id).status = "disabled"
    with pytest.raises(AppException):
        grant(d, "ai_100", "disabled-grant")
    with d.sessions() as session:
        assert session.get(AllowanceGrant, "disabled-grant") is None


def test_operator_cli_and_http_share_audit_and_idempotency(admin_data, monkeypatch, capsys):
    import json
    from app.modules.billing import manage
    from app.modules.billing import service as billing_service
    d = admin_data
    monkeypatch.setattr(manage, "SessionLocal", d.sessions)
    monkeypatch.setattr(billing_service, "resume_queued", AsyncMock(return_value=0))
    args = ["grant", "--user-id", str(d.target.id), "--product", "ai_100", "--key", "cli-grant",
            "--actor", "trusted-operator", "--reason", "人工补发"]
    assert manage.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["payment_verified"] is False and not first["duplicate"]
    assert manage.main(args) == 0
    assert json.loads(capsys.readouterr().out)["duplicate"]
    with d.sessions() as session:
        audit = session.scalar(select(AuditEvent))
        assert audit.actor_kind == "operator" and audit.actor_user_id is None
        assert audit.reason == "人工补发" and audit.actor_label == "trusted-operator"
    # The same global key cannot be claimed by a different operator.
    args[-3] = "another-operator"
    assert manage.main(args) == 1
    assert "IDEMPOTENCY_CONFLICT" in capsys.readouterr().out


def test_lost_commit_acknowledgement_recovers_from_atomic_success_audit(admin_data, monkeypatch):
    d = admin_data
    bootstrap(d)
    execute = d.service._execute_mutation

    def lost_ack(*args, **kwargs):
        execute(*args, **kwargs)
        raise SQLAlchemyError("simulated acknowledgement loss")

    monkeypatch.setattr(d.service, "_execute_mutation", lost_ack)
    result = grant(d, "ai_100", "lost-ack")
    assert result["duplicate"] and result["product"] == "ai_100"
    with d.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AllowanceGrant)) == 1
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.action == "allowance.grant")))
        assert len(events) == 1 and events[0].outcome == "succeeded"


def test_unresolved_database_failure_is_audited_as_unknown_not_failed(admin_data, monkeypatch):
    d = admin_data
    bootstrap(d)

    def unavailable(*args, **kwargs):
        raise SQLAlchemyError("secret connection information")

    monkeypatch.setattr(d.service, "_execute_mutation", unavailable)
    with pytest.raises(SQLAlchemyError):
        grant(d, "ai_100", "uncertain")
    with d.sessions() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "allowance.grant"))
        assert event.outcome == "unknown" and event.result["error_code"] == "DATABASE_OUTCOME_UNKNOWN"
        assert "secret" not in str(event.result) and session.get(AllowanceGrant, "uncertain") is None
