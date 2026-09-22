"""Seed operational plan rows. Idempotent: safe to re-run."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.db.models import Plan

PLANS = [
    {
        "code": "free",
        "name": "免费版",
        "price_cents": 0,
        "currency": "CNY",
        "duration_days": None,
        "features": {
            "download": True,
            "audio_extract": False,
            "transcribe": False,
            "summarize": False,
            "mindmap": False,
            "ask": False,
        },
        "max_daily_downloads": 3,
        "max_video_duration_seconds": 1800,
        "is_active": True,
        "sort_order": 0,
    },
    {
        "code": "monthly",
        "name": "包月会员",
        "price_cents": 1900,
        "currency": "CNY",
        "duration_days": 31,
        "features": {
            "download": True,
            "audio_extract": True,
            "transcribe": True,
            "summarize": True,
            "mindmap": True,
            "ask": False,
        },
        "max_daily_downloads": 100,
        "max_video_duration_seconds": 14400,
        "is_active": True,
        "sort_order": 1,
    },
    {
        "code": "yearly",
        "name": "包年会员",
        "price_cents": 19900,
        "currency": "CNY",
        "duration_days": 365,
        "features": {
            "download": True,
            "audio_extract": True,
            "transcribe": True,
            "summarize": True,
            "mindmap": True,
            "ask": False,
        },
        "max_daily_downloads": 100,
        "max_video_duration_seconds": 14400,
        "is_active": True,
        "sort_order": 2,
    },
]


async def seed_plans(session: AsyncSession) -> list[str]:
    touched: list[str] = []
    for data in PLANS:
        row = (
            await session.execute(select(Plan).where(Plan.code == data["code"]))
        ).scalar_one_or_none()
        if row is None:
            session.add(Plan(**data))
            touched.append(f"insert:{data['code']}")
        else:
            for key, value in data.items():
                setattr(row, key, value)
            touched.append(f"update:{data['code']}")
    await session.commit()
    return touched


async def main() -> None:
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        touched = await seed_plans(session)
    await engine.dispose()
    print("\n".join(touched))


if __name__ == "__main__":
    asyncio.run(main())
