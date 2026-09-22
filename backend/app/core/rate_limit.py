"""IP sliding-window rate limiting over Redis. 429 + Retry-After on excess."""

import time

import redis.asyncio as aredis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from backend.app.core.config import settings
from backend.app.core.errors import error_response

WINDOW_SECONDS = 60

# (method, path prefix) -> per-minute budget key
AUTH_PATHS = (
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
)
PARSE_PATH = "/api/v1/videos/parse"


def _client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


async def _hit(redis: aredis.Redis, key: str, limit: int) -> tuple[bool, int]:
    """Sliding window with a sorted set. Returns (allowed, retry_after_seconds)."""
    now = time.time()
    window_start = now - WINDOW_SECONDS
    async with redis.pipeline() as pipe:
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now:.6f}": now})
        pipe.expire(key, WINDOW_SECONDS + 5)
        _, count, _, _ = await pipe.execute()
    if count >= limit:
        oldest = await redis.zrange(key, 0, 0, withscores=True)
        retry_after = WINDOW_SECONDS
        if oldest:
            retry_after = max(1, int(oldest[0][1] + WINDOW_SECONDS - now))
        return False, retry_after
    return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        limit = 0
        if request.method == "POST" and any(path == p for p in AUTH_PATHS):
            limit = settings.ip_rate_auth_per_min
        elif request.method == "POST" and path == PARSE_PATH:
            limit = (
                settings.ip_rate_parse_user_per_min
                if request.headers.get("authorization")
                else settings.ip_rate_parse_anon_per_min
            )
        if limit <= 0:
            return await call_next(request)
        redis = aredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        try:
            allowed, retry_after = await _hit(redis, f"rl:{_client_ip(request)}:{path}", limit)
        finally:
            await redis.aclose()
        if not allowed:
            resp = error_response("rate_limited", "请求过于频繁，请稍后再试")
            resp.headers["Retry-After"] = str(retry_after)
            return resp
        return await call_next(request)
