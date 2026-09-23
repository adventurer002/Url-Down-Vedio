"""Phase6: plans display, order snapshot, manual grant, expiry."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.api.deps import get_session
from backend.app.db.base import Base
from backend.app.db.models import Order, Subscription, User
from backend.app.main import app
from backend.app.services import billing as billing_service
from backend.app.services import permission as perm

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


@pytest.fixture()
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DB_URL)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as s:
        from backend.app.db.seed import seed_plans

        await seed_plans(s)


async def _user(
    maker: async_sessionmaker[AsyncSession], email: str, admin: bool = False
) -> User:
    from backend.app.services import auth_service as auth

    async with maker() as s:
        user, _, _ = await auth.register_user(s, email, "password123", "t")
        user.is_admin = admin
        await s.commit()
        await s.refresh(user)
        return user


async def test_grant_flips_ai_entitlement(maker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(maker)
    user = await _user(maker, "member@example.com")
    async with maker() as s:
        before = await perm.check_feature(s, user.id, "transcribe")
        assert not before.allowed and before.code == "permission_denied"
        plan = await billing_service.get_plan_by_code(s, "monthly")
        sub = await billing_service.activate_subscription(s, user.id, plan)
        assert sub.status == "active" and sub.expires_at > datetime.now(UTC)
        after = await perm.check_feature(s, user.id, "transcribe")
        assert after.allowed
        expired = await billing_service.expire_due(s)
        assert expired == {"subscriptions": 0, "orders": 0}


async def test_expiry_revokes_entitlement(maker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(maker)
    user = await _user(maker, "exp@example.com")
    async with maker() as s:
        plan = await billing_service.get_plan_by_code(s, "monthly")
        sub = await billing_service.activate_subscription(s, user.id, plan)
        sub.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
        result = await billing_service.expire_due(s)
        assert result["subscriptions"] == 1
        check = await perm.check_feature(s, user.id, "transcribe")
        assert not check.allowed
        reloaded = await s.get(Subscription, sub.id)
        assert reloaded is not None and reloaded.status == "expired"


async def test_order_amount_snapshot(maker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(maker)
    user = await _user(maker, "buyer@example.com")
    async with maker() as s:
        order = await billing_service.create_order(s, user.id, "monthly")
        assert order.status == "pending"
        assert order.expired_at is not None
        first_amount = order.amount_cents
        assert first_amount > 0
        plan = await billing_service.get_plan_by_code(s, "monthly")
        plan.price_cents = first_amount + 100
        await s.commit()
        paid, sub = await billing_service.grant_for_order(s, order.id)
        assert paid.status == "paid" and paid.paid_at is not None
        assert paid.amount_cents == first_amount
        assert sub.plan_id == plan.id


async def test_stale_pending_order_expires(maker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(maker)
    user = await _user(maker, "stale@example.com")
    async with maker() as s:
        order = await billing_service.create_order(s, user.id, "monthly")
        order.expired_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
        result = await billing_service.expire_due(s)
        assert result["orders"] == 1
        reloaded = await s.get(Order, order.id)
        assert reloaded is not None and reloaded.status == "expired"


def test_plans_orders_grant_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)

    async def setup() -> None:
        async with engine.connect() as conn:
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            await conn.commit()
        mk = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with mk() as s:
            from backend.app.db.seed import seed_plans
            from backend.app.services import auth_service as auth

            await seed_plans(s)
            admin, admin_access, _ = await auth.register_user(
                s, "admin@example.com", "password123", "admin"
            )
            admin.is_admin = True
            await s.commit()
            _, buyer_access, _ = await auth.register_user(
                s, "buyer2@example.com", "password123", "buyer"
            )
            await s.commit()
            admin_token, buyer_token = admin_access, buyer_access

        async def override() -> AsyncIterator[AsyncSession]:
            async with mk() as s:
                yield s

        app.dependency_overrides[get_session] = override

        from backend.app.providers.payment import PayParams
        from backend.app.services import billing as billing_service

        class FakeChannel:
            provider = "stripe"

            async def create_payment(self, order_no, amount_cents, currency, subject):  # type: ignore[no-untyped-def]
                return PayParams(provider="stripe", pay_url=f"https://pay.example/{order_no}")

        monkeypatch.setattr(billing_service, "get_provider", lambda name: FakeChannel())
        with TestClient(app) as c:
            r = c.get("/api/v1/plans")
            assert r.status_code == 200
            codes = [p["code"] for p in r.json()]
            assert codes == ["free", "monthly", "yearly"]

            r = c.post(
                "/api/v1/orders",
                json={"plan_code": "monthly", "provider": "stripe"},
                headers={"Authorization": f"Bearer {buyer_token}"},
            )
            assert r.status_code == 201, r.text
            assert r.json()["pay_params"]["pay_url"].startswith("https://pay.example/")
            assert r.json()["amount_cents"] > 0
            order_no = r.json()["order_no"]
            first_amount = r.json()["amount_cents"]

            async def bump() -> None:
                async with mk() as s2:
                    plan = await billing_service.get_plan_by_code(s2, "monthly")
                    plan.price_cents = first_amount + 5000
                    await s2.commit()

            await bump()
            r = c.get(
                f"/api/v1/orders/{order_no}",
                headers={"Authorization": f"Bearer {buyer_token}"},
            )
            assert r.json()["amount_cents"] == first_amount

            r = c.post(
                "/api/v1/admin/grant",
                json={"email": "buyer2@example.com", "plan_code": "monthly"},
                headers={"Authorization": f"Bearer {buyer_token}"},
            )
            assert r.status_code == 403

            r = c.post(
                "/api/v1/admin/grant",
                json={"email": "buyer2@example.com", "plan_code": "monthly"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert r.status_code == 200, r.text

            r = c.get(
                "/api/v1/users/me",
                headers={"Authorization": f"Bearer {buyer_token}"},
            )
            assert r.json()["plan_code"] == "monthly"
        app.dependency_overrides.clear()

    import asyncio

    asyncio.run(setup())
