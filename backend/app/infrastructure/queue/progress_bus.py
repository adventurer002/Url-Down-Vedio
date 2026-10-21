import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import json
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.exceptions import AppException

TERMINAL = frozenset({"completed", "failed", "cancelled", "expired"})

# Throttle, sequence allocation, hash snapshot and publish are ONE atomic operation.
# The sequence key deliberately outlives the snapshot to avoid resetting seq on TTL.
PUBLISH_SCRIPT = """
local previous = redis.call('HGET', KEYS[1], 'status')
local terminal = {completed=true, failed=true, cancelled=true, expired=true}
local ranks = {queued=0, running=1, processing=2}
if previous and terminal[previous] then return false end
if previous and ranks[previous] and ranks[ARGV[2]] and ranks[ARGV[2]] < ranks[previous] then return false end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local last = tonumber(redis.call('HGET', KEYS[1], 'last_emit_ms') or '0')
if previous == ARGV[2] and now - last < tonumber(ARGV[8]) then return false end
local progress = math.max(tonumber(ARGV[3]), tonumber(redis.call('HGET', KEYS[1], 'progress') or '0'))
local seq = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[1], 'task_id', ARGV[1], 'status', ARGV[2], 'progress', progress,
  'seq', seq, 'stage', ARGV[4], 'error_code', ARGV[5], 'updated_at', ARGV[6], 'last_emit_ms', now)
redis.call('EXPIRE', KEYS[1], ARGV[7])
local event = cjson.encode({task_id=ARGV[1], status=ARGV[2], progress=progress,
  seq=seq, stage=ARGV[4], error_code=ARGV[5] == '' and cjson.null or ARGV[5], updated_at=ARGV[6]})
redis.call('PUBLISH', KEYS[3], event)
return event
"""


def unavailable() -> AppException:
    return AppException("PROGRESS_UNAVAILABLE", "任务进度服务暂不可用，请稍后重试", 503)


class ProgressSubscription:
    def __init__(self, pubsub):
        self.pubsub = pubsub

    async def receive(self, timeout: float = 0.5) -> dict | None:
        try:
            async with asyncio.timeout(2):
                message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if message and message["type"] == "message":
                return json.loads(message["data"])
            return None
        except (RedisError, TimeoutError, ValueError):
            raise unavailable() from None

    async def close(self):
        try:
            async with asyncio.timeout(2):
                await self.pubsub.aclose()
        except (RedisError, TimeoutError):
            pass


class ProgressBus:
    def __init__(self, redis: Redis, ttl: int = 86400, throttle_ms: int = 500):
        if not 500 <= throttle_ms <= 1000 or ttl < 1:
            raise ValueError("invalid progress retention or throttle")
        self.redis = redis
        self.ttl = ttl
        self.throttle_ms = throttle_ms

    async def publish(self, task_id: UUID, status: str, progress: int, stage: str | None = None,
                      error_code: str | None = None) -> dict | None:
        if status not in {"queued", "running", "processing", *TERMINAL} or not 0 <= progress <= 100:
            raise ValueError("invalid task progress")
        try:
            async with asyncio.timeout(2):
                raw = await self.redis.eval(
                    PUBLISH_SCRIPT, 3, f"task:snapshot:{task_id}", f"task:seq:{task_id}", f"task:events:{task_id}",
                    str(task_id), status, progress, stage or status, error_code or "",
                    datetime.now(timezone.utc).isoformat(), self.ttl, self.throttle_ms,
                )
            return json.loads(raw) if raw else None
        except (RedisError, TimeoutError):
            raise unavailable() from None

    async def snapshot(self, task_id: UUID) -> dict | None:
        try:
            async with asyncio.timeout(2):
                values = await self.redis.hgetall(f"task:snapshot:{task_id}")
            if not values:
                return None
            return {"task_id": values["task_id"], "status": values["status"],
                    "progress": int(values["progress"]), "seq": int(values["seq"]),
                    "stage": values["stage"], "error_code": values["error_code"] or None,
                    "updated_at": values["updated_at"]}
        except (RedisError, TimeoutError, ValueError, KeyError):
            raise unavailable() from None

    async def subscribe(self, task_id: UUID) -> ProgressSubscription:
        pubsub = self.redis.pubsub()
        subscription = ProgressSubscription(pubsub)
        try:
            async with asyncio.timeout(2):
                await pubsub.subscribe(f"task:events:{task_id}")
                # subscribe() sends the command; only its ACK proves it is active.
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=0.5)
                    if message and message["type"] == "subscribe":
                        return subscription
        except (RedisError, TimeoutError):
            await subscription.close()
            raise unavailable() from None
        except BaseException:
            await subscription.close()
            raise

    async def request_task_cancel(self, task_id: UUID, persist: Callable[[], Awaitable[bool]]) -> bool:
        # Database CAS decides whether cancellation is legal before setting Redis.
        accepted = await persist()
        if accepted:
            try:
                async with asyncio.timeout(2):
                    await self.redis.set(f"task:cancel:{task_id}", "1", ex=self.ttl)
            except (RedisError, TimeoutError):
                # PG remains durable; workers also check it if Redis is down.
                raise unavailable() from None
        return accepted

    async def is_cancel_requested(self, task_id: UUID, persisted: Callable[[], Awaitable[bool]]) -> bool:
        try:
            async with asyncio.timeout(2):
                if await self.redis.exists(f"task:cancel:{task_id}"):
                    return True
        except (RedisError, TimeoutError):
            pass
        return await persisted()
