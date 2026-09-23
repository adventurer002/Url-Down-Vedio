"""Phase9: admin moderation, audit trail, non-admin rejection."""

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.api.deps import get_session
from backend.app.db.base import Base
from backend.app.main import app

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


class AdminCtx(dict):  # type: ignore[type-arg]
    """Test fixture bag: client, admin/user tokens, subscription id."""

    @property
    def http(self) -> TestClient:
        return self["client"]  # type: ignore[no-any-return]

    @property
    def admin(self) -> str:
        return str(self["admin"])

    @property
    def user(self) -> str:
        return str(self["user"])

    @property
    def sub(self) -> str:
        return str(self["sub"])


@pytest.fixture()
async def client() -> AsyncIterator[AdminCtx]:
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        from backend.app.db.seed import seed_plans
        from backend.app.services import auth_service as auth
        from backend.app.services import billing as billing_service

        await seed_plans(s)
        admin, admin_access, _ = await auth.register_user(
            s, "root@example.com", "password123", "root"
        )
        admin.is_admin = True
        user, user_access, _ = await auth.register_user(
            s, "plain@example.com", "password123", "plain"
        )
        plan = await billing_service.get_plan_by_code(s, "monthly")
        sub = await billing_service.activate_subscription(s, user.id, plan)
        sub_id = str(sub.id)
        await s.commit()

    async def override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override
    app.state.audit_session_maker = maker
    with TestClient(app) as c:
        yield AdminCtx(client=c, admin=admin_access, user=user_access, sub=sub_id)
    app.dependency_overrides.clear()
    del app.state.audit_session_maker
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_non_admin_rejected(client: AdminCtx) -> None:
    c, user_token = client.http, client.user
    for method, path in [
        ("get", "/api/v1/admin/users"),
        ("get", "/api/v1/admin/orders"),
        ("get", "/api/v1/admin/usage/summary"),
        ("get", "/api/v1/admin/audit-logs"),
    ]:
        r = c.request(method, path, headers=_auth(user_token))
        assert r.status_code == 403, (method, path)
        assert r.json()["error"]["code"] == "permission_denied"
    r = c.request("get", "/api/v1/admin/users")
    assert r.status_code == 401


def test_admin_lists_and_summary(client: AdminCtx) -> None:
    c, admin_token = client.http, client.admin
    r = c.get("/api/v1/admin/users", headers=_auth(admin_token), params={"search": "plain"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["email"] == "plain@example.com"
    r = c.get("/api/v1/admin/orders", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["total"] == 0
    r = c.get("/api/v1/admin/usage/summary", headers=_auth(admin_token))
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_revoke_and_audit(client: AdminCtx) -> None:
    c, admin_token, sub_id = client.http, client.admin, client.sub
    r = c.post(
        f"/api/v1/admin/subscriptions/{sub_id}/revoke", headers=_auth(admin_token)
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    r = c.post(
        f"/api/v1/admin/subscriptions/{sub_id}/revoke", headers=_auth(admin_token)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    r = c.get("/api/v1/admin/audit-logs", headers=_auth(admin_token))
    assert r.status_code == 200
    actions = [e["action"] for e in r.json()["items"]]
    assert "revoke" in actions


def test_cleanup_preview(client: AdminCtx) -> None:
    c, admin_token = client.http, client.admin
    r = c.post("/api/v1/admin/media/cleanup", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["expired_pending"] == 0
