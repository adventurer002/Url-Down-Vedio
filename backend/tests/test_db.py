"""Phase2: models + seed against an isolated test database (compose postgres)."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test",
)

from backend.app.db.base import Base
from backend.app.db.models import Plan, UsageRecord, User
from backend.app.db.seed import seed_plans


async def _ensure_db() -> None:
    admin_url = TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        exists = await conn.scalar(
            sa.text("SELECT 1 FROM pg_database WHERE datname='downvedio_test'")
        )
        if not exists:
            await conn.execute(sa.text("CREATE DATABASE downvedio_test"))
    await engine.dispose()


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    await _ensure_db()
    engine = create_async_engine(TEST_DB_URL)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


async def test_tables_exist(session: AsyncSession) -> None:
    conn = await session.connection()
    tables = [
        t
        for t in await conn.run_sync(
            lambda sync_conn: sa.inspect(sync_conn).get_table_names()
        )
        if t != "alembic_version"
    ]
    assert len(tables) == 14, tables  # 13 + audit_logs (Phase9)
    for name in ("users", "plans", "videos", "usage_records", "audit_logs"):
        row = await session.execute(sa.text(f"SELECT id, created_at, updated_at FROM {name} LIMIT 0"))
        assert set(row.keys()) >= {"id", "created_at", "updated_at"}


async def test_seed_idempotent(session: AsyncSession) -> None:
    first = await seed_plans(session)
    assert sorted(first) == ["insert:free", "insert:monthly", "insert:yearly"]
    second = await seed_plans(session)
    assert sorted(second) == ["update:free", "update:monthly", "update:yearly"]
    codes = (await session.execute(select(Plan.code))).scalars().all()
    assert sorted(codes) == ["free", "monthly", "yearly"]
    free = (await session.execute(select(Plan).where(Plan.code == "free"))).scalar_one()
    assert free.max_daily_downloads == 3
    assert free.features["transcribe"] is False


async def test_usage_check_constraint(session: AsyncSession) -> None:
    user = User(email="t@example.com", password_hash="x", nickname="t")
    session.add(user)
    await session.commit()
    async with session.begin_nested():
        session.add(
            UsageRecord(user_id=user.id, action="summarize", cost_cents=-1)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
    session.add(
        UsageRecord(
            user_id=user.id,
            action="summarize",
            ref_id=uuid.uuid4(),
            llm_input_tokens=10,
            llm_output_tokens=5,
            cost_cents=3,
        )
    )
    await session.commit()
