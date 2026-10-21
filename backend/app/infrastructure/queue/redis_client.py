import asyncio

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import settings

READINESS_TIMEOUT = 1.5
redis_client = Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,
    retry=Retry(NoBackoff(), 0),
)


async def check_redis_ready() -> bool:
    try:
        async with asyncio.timeout(READINESS_TIMEOUT):
            return bool(await redis_client.ping())
    except Exception:
        return False
