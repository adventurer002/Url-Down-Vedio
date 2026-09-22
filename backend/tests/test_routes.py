"""Route tests: envelope codes, 202 async, cancel rules, SSE replay, file gating."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.api.deps import get_session
from backend.app.core.security import create_access_token
from backend.app.db.base import Base
from backend.app.db.models import DownloadTask, MediaFile, User, Video
from backend.app.main import app
from backend.app.worker import tasks as worker_tasks

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


from collections.abc import AsyncIterator


async def _make_user(
    maker: async_sessionmaker[AsyncSession], email: str = "u@example.com"
) -> tuple[User, dict[str, str]]:
    """Create a user directly and return (user, auth headers)."""
    from backend.app.services import auth_service as auth

    async with maker() as s:
        user, _, _ = await auth.register_user(s, email, "password123", "tester")
        headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}
        return user, headers


@pytest.fixture()
async def client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[TestClient, async_sessionmaker[AsyncSession]]]:

    # NullPool: TestClient serves requests on its own event loop, so no
    # connection may be shared across loops.
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
    with TestClient(app) as c:
        yield c, maker
    app.dependency_overrides.clear()
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


def test_parse_rejects_private_url(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    r = c.post("/api/v1/videos/parse", json={"url": "http://192.168.0.1/x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "url_not_allowed"


async def test_parse_accepts_and_replays(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from backend.app.core import url_safety

    c, maker = client
    monkeypatch.setattr(
        url_safety, "validate_url", lambda url: url_safety.SafeURL(url, "x")
    )
    _, headers = await _make_user(maker)
    r = c.post("/api/v1/videos/parse", json={"url": "https://public.example.com/v"}, headers=headers)
    assert r.status_code == 202, r.text
    vid = r.json()["video_id"]
    r2 = c.get(f"/api/v1/videos/{vid}", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "parsing"
    r3 = c.get(f"/api/v1/videos/{vid}")
    assert r3.status_code == 404


async def test_download_requires_ready(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from backend.app.core import url_safety

    c, maker = client
    monkeypatch.setattr(
        url_safety, "validate_url", lambda url: url_safety.SafeURL(url, "x")
    )
    _, headers = await _make_user(maker)
    r0 = c.post("/api/v1/downloads", json={"video_id": str(uuid.uuid4()), "format_id": "hd"})
    assert r0.status_code == 401
    r = c.post("/api/v1/videos/parse", json={"url": "https://public.example.com/v"}, headers=headers)
    vid = r.json()["video_id"]
    r2 = c.post("/api/v1/downloads", json={"video_id": vid, "format_id": "hd"}, headers=headers)
    assert r2.status_code == 502
    assert r2.json()["error"]["code"] == "download_failed"

    async with maker() as s:
        v = await s.get(Video, uuid.UUID(vid))
        assert v is not None
        v.status = "ready"
        v.formats = [{"format_id": "hd", "resolution": "720p"}]
        await s.commit()
    r3 = c.post("/api/v1/downloads", json={"video_id": vid, "format_id": "hd"}, headers=headers)
    assert r3.status_code == 202, r3.text
    task_id = r3.json()["task_id"]
    r4 = c.get(f"/api/v1/downloads/{task_id}", headers=headers)
    assert r4.json()["status"] == "queued"
    assert r4.json()["quality"] == "720p"


async def test_cancel_rules(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from backend.app.core import url_safety
    from backend.app.worker.celery_app import celery_app

    c, maker = client
    monkeypatch.setattr(
        url_safety, "validate_url", lambda url: url_safety.SafeURL(url, "x")
    )
    monkeypatch.setattr(celery_app.control, "revoke", lambda *a, **k: None)
    import redis as sync_redis

    real_from_url = sync_redis.Redis.from_url
    monkeypatch.setattr(
        sync_redis.Redis, "from_url",
        lambda url: real_from_url("redis://localhost:6379/0"),
    )
    user, headers = await _make_user(maker)
    r = c.post("/api/v1/videos/parse", json={"url": "https://public.example.com/v"}, headers=headers)
    vid = r.json()["video_id"]

    async with maker() as s:
        v = await s.get(Video, uuid.UUID(vid))
        assert v is not None
        v.status = "ready"
        await s.commit()
        t = DownloadTask(
            user_id=user.id, video_id=v.id, format_id="hd", quality="hd",
            status="uploading", progress=90, celery_task_id="",
        )
        s.add(t)
        await s.commit()
        uploading_id = str(t.id)
    r2 = c.post(f"/api/v1/downloads/{uploading_id}/cancel", headers=headers)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "task_not_cancellable"


async def test_sse_replays_terminal_state(client) -> None:  # type: ignore[no-untyped-def]
    c, maker = client
    user, headers = await _make_user(maker)

    async with maker() as s:
            v = Video(
                source_url="https://x.example/v", platform="t", platform_video_id="1",
                title="t", webpage_url="https://x.example/v", status="ready",
            )
            s.add(v)
            await s.flush()
            t = DownloadTask(
                user_id=None, video_id=v.id, format_id="hd", quality="hd",
                status="completed", progress=100, celery_task_id="",
                finished_at=datetime.now(UTC),
            )
            t.user_id = user.id
            s.add(t)
            await s.flush()
            s.add(
                MediaFile(
                    owner_user_id=user.id, task_id=t.id, video_id=v.id, kind="video",
                    format="mp4", size_bytes=8, storage_backend="local",
                    storage_key="k", expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await s.commit()
            task_id = str(t.id)
    with c.stream("GET", f"/api/v1/downloads/{task_id}/events", headers=headers) as r:
        assert r.status_code == 200
        body = r.read().decode()
    assert "event: progress" in body
    assert "event: end" in body
    assert task_id in body


async def test_file_requires_completed(client) -> None:  # type: ignore[no-untyped-def]
    c, maker = client
    user, headers = await _make_user(maker)

    async with maker() as s:
            v = Video(
                source_url="https://x.example/v", platform="t", platform_video_id="1",
                title="t", webpage_url="https://x.example/v", status="ready",
            )
            s.add(v)
            await s.flush()
            t = DownloadTask(
                user_id=user.id, video_id=v.id, format_id="hd", quality="hd",
                status="downloading", progress=10, celery_task_id="",
            )
            s.add(t)
            await s.commit()
            task_id = str(t.id)
    r = c.get(f"/api/v1/downloads/{task_id}/file", headers=headers)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "download_failed"
