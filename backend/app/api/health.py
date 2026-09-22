from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001 - readiness probe must report, not raise
        checks["db"] = f"error: {e}"
    try:
        r = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001 - readiness probe must report, not raise
        checks["redis"] = f"error: {e}"
    ok = all(v == "ok" for v in checks.values())
    return {"ready": ok, "checks": checks}
