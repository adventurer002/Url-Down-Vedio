"""Grant admin to a user by email. Usage: .venv/bin/python scripts/make_admin.py user@example.com"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.db.models import User


async def main(email: str) -> None:
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            print(f"user not found: {email}")
            raise SystemExit(1)
        user.is_admin = True
        await session.commit()
        print(f"admin granted: {email}")
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1]))
