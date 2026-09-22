"""Smoke test for Phase1 health endpoints."""

from fastapi.testclient import TestClient

from backend.app.main import app


def test_healthz() -> None:
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")
