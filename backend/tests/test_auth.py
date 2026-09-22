"""Phase4: auth, quotas, rate limits."""

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.api.deps import get_session
from backend.app.db.base import Base
from backend.app.db.models import UsageRecord, Video
from backend.app.main import app
from backend.app.worker import tasks as worker_tasks

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


async def _allow_all(redis: object, key: str, limit: int) -> tuple[bool, int]:
    return True, 0


@pytest.fixture()
async def client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[TestClient, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as seed_session:
        from backend.app.db.seed import seed_plans

        await seed_plans(seed_session)

    async def override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr(
        worker_tasks.parse_video, "delay", lambda vid: SimpleNamespace(id="celery-p")
    )
    monkeypatch.setattr(
        worker_tasks.download_video, "delay", lambda tid: SimpleNamespace(id="celery-d")
    )
    monkeypatch.setattr("backend.app.core.rate_limit._hit", _allow_all)
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


def _register(c: TestClient, email: str = "u@example.com") -> dict[str, object]:
    r = c.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "nickname": "tester"},
    )
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    assert body["access_token"] and body["refresh_token"]
    return body


def test_register_login_me_refresh_rotation(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    body = _register(c)
    user = body["user"]
    assert isinstance(user, dict) and user["email"] == "u@example.com"
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    r = c.post(
        "/api/v1/auth/login",
        json={"email": "u@example.com", "password": "password123"},
    )
    assert r.status_code == 200

    r = c.post(
        "/api/v1/auth/login", json={"email": "u@example.com", "password": "wrongpass1"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"

    r = c.get("/api/v1/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["plan_code"] == "free"
    assert r.json()["today_downloads_quota"] == 3

    r = c.get("/api/v1/users/me")
    assert r.status_code == 401

    r = c.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200
    new_refresh = r.json()["refresh_token"]

    r = c.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401

    r = c.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert r.status_code == 200

    r = c.post(
        "/api/v1/auth/register",
        json={"email": "u@example.com", "password": "password123"},
    )
    assert r.status_code == 422


async def test_download_quota_exceeded(client) -> None:  # type: ignore[no-untyped-def]
    c, maker = client
    body = _register(c)
    user = body["user"]
    assert isinstance(user, dict)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    user_id = uuid.UUID(str(user["id"]))

    async with maker() as s:
        s.add(
            Video(
                source_url="https://x.example/v", platform="t", platform_video_id="1",
                title="t", webpage_url="https://x.example/v", status="ready",
                duration_seconds=60, user_id=user_id,
                formats=[{"format_id": "hd", "resolution": "720p"}],
            )
        )
        for _ in range(3):
            s.add(UsageRecord(user_id=user_id, action="download"))
        await s.commit()
        vid = await s.scalar(sa.select(Video.id))

    r = c.post(
        "/api/v1/downloads", json={"video_id": str(vid), "format_id": "hd"},
        headers=headers,
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"

    r = c.get("/api/v1/usage", headers=headers)
    assert r.status_code == 200
    assert r.json()["today_downloads_used"] == 3


async def test_download_duration_limit(client) -> None:  # type: ignore[no-untyped-def]
    c, maker = client
    body = _register(c, "long@example.com")
    user = body["user"]
    assert isinstance(user, dict)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    user_id = uuid.UUID(str(user["id"]))

    async with maker() as s:
        s.add(
            Video(
                source_url="https://x.example/v", platform="t", platform_video_id="1",
                title="t", webpage_url="https://x.example/v", status="ready",
                duration_seconds=3600, user_id=user_id,
                formats=[{"format_id": "hd", "resolution": "720p"}],
            )
        )
        await s.commit()
        vid = await s.scalar(sa.select(Video.id))

    r = c.post(
        "/api/v1/downloads", json={"video_id": str(vid), "format_id": "hd"},
        headers=headers,
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"


def test_ip_rate_limit_on_register() -> None:
    """Real sliding window: no mocks here, own app instance usage via main client."""
    import uuid as uuid_mod

    import redis as sync_redis

    from backend.app.core.config import settings

    r = sync_redis.Redis.from_url("redis://localhost:6379/0")
    r.flushdb()
    tag = uuid_mod.uuid4().hex[:8]

    orig_auth = settings.ip_rate_auth_per_min
    settings.ip_rate_auth_per_min = 3
    try:
        from backend.app.main import app as live_app

        with TestClient(live_app) as c:
            codes = []
            retry_after = None
            for i in range(5):
                resp = c.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"rl{i}-{tag}@example.com",
                        "password": "password123",
                    },
                )
                codes.append(resp.status_code)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    assert resp.json()["error"]["code"] == "rate_limited"
    finally:
        settings.ip_rate_auth_per_min = orig_auth
    assert codes[:3] == [201, 201, 201], codes
    assert codes[3] == 429 and codes[4] == 429, codes
    assert retry_after is not None
