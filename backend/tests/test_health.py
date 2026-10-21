import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1 import router as health
from app.core.exceptions import AppException
from app.infrastructure.database import session as database
from app.infrastructure.queue import redis_client as redis_module
from app.infrastructure.queue.celery_app import celery_app, system_ping
from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as test_client:
            yield test_client


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"].startswith("req_")
    assert response.headers["X-Request-ID"] == body["error"]["request_id"]


async def test_live_does_not_probe_dependencies(client, monkeypatch):
    db = AsyncMock(side_effect=AssertionError("must not probe"))
    redis = AsyncMock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(health, "check_db_ready", db)
    monkeypatch.setattr(health, "check_redis_ready", redis)
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "alive"}}
    assert response.headers["X-Request-ID"].startswith("req_")
    db.assert_not_awaited()
    redis.assert_not_awaited()


@pytest.mark.parametrize("db_ok,redis_ok", [(True, True), (False, True), (True, False), (False, False)])
async def test_ready(client, monkeypatch, db_ok, redis_ok):
    monkeypatch.setattr(health, "check_db_ready", AsyncMock(return_value=db_ok))
    monkeypatch.setattr(health, "check_redis_ready", AsyncMock(return_value=redis_ok))
    response = await client.get("/api/v1/health/ready")
    if db_ok and redis_ok:
        assert response.status_code == 200
        assert response.json() == {"data": {"status": "ready", "database": "ready", "redis": "ready"}}
    else:
        assert_error(response, 503, "DEPENDENCY_UNAVAILABLE")
        assert f"PostgreSQL: {'ready' if db_ok else 'unavailable'}" in response.json()["error"]["message"]
        assert f"Redis: {'ready' if redis_ok else 'unavailable'}" in response.json()["error"]["message"]
        assert "videohub_pass" not in response.text


async def test_not_found(client):
    assert_error(await client.get("/api/v1/missing"), 404, "NOT_FOUND")


@pytest.mark.parametrize("kind,status,code", [
    ("custom", 409, "EXAMPLE_CONFLICT"),
    ("unexpected", 500, "INTERNAL_SERVER_ERROR"),
    ("validation", 422, "VALIDATION_ERROR"),
])
async def test_exception_handlers(kind, status, code):
    app = create_app()

    @app.get("/api/v1/test-error")
    def raise_error(value: int):
        if kind == "custom":
            raise AppException("EXAMPLE_CONFLICT", "示例冲突", 409)
        raise RuntimeError("secret connection password")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/test-error", params={"value": "bad" if kind == "validation" else 1})
    assert_error(response, status, code)
    assert "secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_database_probe(monkeypatch, failure):
    connection = AsyncMock()
    connection.execute.side_effect = RuntimeError("secret") if failure else None
    context_manager = AsyncMock()
    context_manager.__aenter__.return_value = connection
    monkeypatch.setattr(database, "readiness_engine", Mock(connect=Mock(return_value=context_manager)))
    assert await database.check_db_ready() is (not failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_redis_probe(monkeypatch, failure):
    ping = AsyncMock(return_value=True, side_effect=RuntimeError("secret") if failure else None)
    monkeypatch.setattr(redis_module.redis_client, "ping", ping)
    assert await redis_module.check_redis_ready() is (not failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["database", "redis"])
async def test_probe_total_timeout(monkeypatch, dependency):
    async def slow_io(*args):
        await asyncio.sleep(10)

    if dependency == "database":
        monkeypatch.setattr(database, "READINESS_TIMEOUT", 0.02)
        context_manager = AsyncMock()
        context_manager.__aenter__.side_effect = slow_io
        monkeypatch.setattr(database, "readiness_engine", Mock(connect=Mock(return_value=context_manager)))
        probe = database.check_db_ready
    else:
        monkeypatch.setattr(redis_module, "READINESS_TIMEOUT", 0.02)
        monkeypatch.setattr(redis_module.redis_client, "ping", slow_io)
        probe = redis_module.check_redis_ready
    assert await asyncio.wait_for(probe(), timeout=0.5) is False


def test_system_ping_registration():
    assert "system.ping" in celery_app.tasks
    assert system_ping.run() == {"pong": True}
