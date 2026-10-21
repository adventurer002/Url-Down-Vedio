import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.infrastructure.database import models  # noqa: F401

READINESS_TIMEOUT = 1.5
CONNECT_ARGS = {"connect_timeout": 1, "options": "-c statement_timeout=1000"}

engine = create_engine(
    settings.DATABASE_URL, pool_pre_ping=True, pool_timeout=1, connect_args=CONNECT_ARGS
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Async psycopg permits cancellation of connection/query I/O within the total budget.
readiness_engine = create_async_engine(
    settings.DATABASE_URL, poolclass=NullPool, connect_args=CONNECT_ARGS
)


def get_db():
    with SessionLocal() as session:
        yield session


async def check_db_ready() -> bool:
    try:
        async with asyncio.timeout(READINESS_TIMEOUT):
            async with readiness_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        # No retries on probes; the next probe is the controlled retry.
        return False
