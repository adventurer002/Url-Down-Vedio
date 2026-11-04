"""Parent-process Redis permits. Children never access Redis under NetworkGuard."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import time
from uuid import uuid4

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers.links import canonical_link, platform_for, resolve_short_link, is_domestic_short_link
from app.core.security import validate_source_url as validate_url
from app.modules.video.parsers.runtime import server_proxy

_active = ContextVar("video_source_operation", default=None)
_metadata_deadline = ContextVar("video_metadata_deadline", default=None)

# All timestamps use Redis TIME, not clocks from different API/Worker processes.
_PERMIT = r"""
local state, meta, done = KEYS[1], KEYS[2], KEYS[3]
local action, token, kind = ARGV[1], ARGV[2], ARGV[3]
local life, window, cooldown, threshold = tonumber(ARGV[4]), tonumber(ARGV[5]), tonumber(ARGV[6]), tonumber(ARGV[7])
local epoch, outcome = tonumber(ARGV[8]), ARGV[9]
local clock = redis.call('TIME')
local now = tonumber(clock[1])*1000 + math.floor(tonumber(clock[2])/1000)
local generation = tonumber(redis.call('HGET', state, 'generation') or '0')
local function unlock()
  if redis.call('GET', meta) == token then redis.call('DEL', meta) end
end
if action == 'begin' then
  local blocked = tonumber(redis.call('HGET', state, 'open_until') or '0')
  if blocked > now then return {'cooldown', tostring(generation)} end
  local probe = redis.call('HGET', state, 'probe') or ''
  local probe_until = tonumber(redis.call('HGET', state, 'probe_until') or '0')
  if probe ~= '' and probe_until > now then return {'cooldown', tostring(generation)} end
  if redis.call('EXISTS', meta) == 1 then return {'busy', tostring(generation)} end
  if probe ~= '' then
    generation = generation + 1
    redis.call('HDEL', state, 'probe', 'probe_until')
  end
  redis.call('HSET', state, 'generation', generation)
  redis.call('SET', meta, token, 'PX', math.min(life, 40000))
  if blocked > 0 then
    redis.call('HSET', state, 'probe', token, 'probe_until', now + math.min(life, 40000))
  end
  redis.call('PEXPIRE', state, life)
  return {'ok', tostring(generation)}
end
unlock()
if action == 'ready' then
  if generation == epoch and redis.call('HGET', state, 'probe') == token then
    generation = generation + 1
    redis.call('HSET', state, 'generation', generation, 'open_until', 0, 'failures', 0, 'last_failure', 0)
    redis.call('HDEL', state, 'probe', 'probe_until')
    return {'ok', tostring(generation)}
  end
  return {'ok', tostring(epoch)}
end
if not redis.call('SET', done, '1', 'PX', life, 'NX') then return {'duplicate', tostring(epoch)} end
if generation ~= epoch then return {'stale', tostring(epoch)} end
local probe = redis.call('HGET', state, 'probe') or ''
if probe ~= '' and probe ~= token then return {'stale', tostring(epoch)} end
if outcome == 'failure' then
  local last = tonumber(redis.call('HGET', state, 'last_failure') or '0')
  local count = tonumber(redis.call('HGET', state, 'failures') or '0')
  if now - last > window then count = 0 end
  count = count + 1
  redis.call('HSET', state, 'failures', count, 'last_failure', now)
  if count >= threshold or probe == token then
    redis.call('HSET', state, 'open_until', now + cooldown, 'generation', generation + 1)
    redis.call('HDEL', state, 'probe', 'probe_until')
  end
elseif outcome == 'success' then
  redis.call('HSET', state, 'failures', 0, 'last_failure', 0)
  if probe == token then
    redis.call('HSET', state, 'open_until', 0, 'generation', generation + 1)
    redis.call('HDEL', state, 'probe', 'probe_until')
  end
elseif probe == token then
  redis.call('HSET', state, 'generation', generation + 1)
  redis.call('HDEL', state, 'probe', 'probe_until')
end
redis.call('PEXPIRE', state, life)
return {'ok', tostring(epoch)}
"""


@contextmanager
def metadata_budget(timeout=35):
    previous = _metadata_deadline.get()
    deadline = time.monotonic() + max(timeout - 5, timeout / 2)
    token = _metadata_deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _metadata_deadline.reset(token)


@dataclass
class SourceLease:
    deadline: float
    metadata_deadline: float
    coordinator: object = None
    bucket: str = ""
    token: str = ""
    generation: int = 0

    @property
    def remaining(self):
        return max(.001, self.deadline - time.monotonic())

    @property
    def metadata_remaining(self):
        return max(.001, self.metadata_deadline - time.monotonic())

    async def metadata_ready(self):
        if self.coordinator is not None:
            reply = await self.coordinator.update(self, "ready")
            self.generation = int(reply[1])


class SourceCoordinator:
    def __init__(self, redis, *, namespace="videohub:source:v1"):
        self.redis, self.namespace = redis, namespace

    def keys(self, lease):
        prefix = f"{self.namespace}:{lease.bucket}"
        return [prefix + ":state", prefix + ":metadata", prefix + ":done:" + lease.token]

    async def update(self, lease, action, outcome="neutral"):
        lifetime = int((settings.SOURCE_FAILURE_WINDOW_SECONDS + settings.SOURCE_COOLDOWN_SECONDS +
                        max(40, lease.remaining) + 10) * 1000)
        try:
            async with asyncio.timeout(1.5):
                return await self.redis.eval(_PERMIT, 3, *self.keys(lease), action, lease.token, "source",
                    lifetime, settings.SOURCE_FAILURE_WINDOW_SECONDS * 1000, settings.SOURCE_COOLDOWN_SECONDS * 1000,
                    settings.SOURCE_FAILURE_THRESHOLD, lease.generation, outcome)
        except (RedisError, OSError, TimeoutError):
            raise AppException("DEPENDENCY_UNAVAILABLE", "视频来源协调服务暂不可用，请稍后重试", 503) from None

    async def acquire(self, bucket, deadline, cancelled=None):
        lease = SourceLease(deadline, min(deadline, time.monotonic() + 30), self, bucket, uuid4().hex)
        while True:
            if cancelled is not None:
                try:
                    async with asyncio.timeout(3):
                        if await cancelled():
                            raise AppException("TASK_CANCELLED", "任务已取消", 409)
                except TimeoutError:
                    raise AppException("DOWNLOAD_CONTROL_TIMEOUT", "取消检查超过执行时限", 503) from None
            if time.monotonic() >= deadline:
                raise AppException("PARSE_BUSY", "平台元数据解析繁忙，请稍后重试", 503)
            reply = await self.update(lease, "begin")
            if reply[0] == "ok":
                lease.generation = int(reply[1])
                return lease
            if reply[0] == "cooldown":
                raise AppException("SOURCE_COOLDOWN", "视频平台暂时冷却中，请稍后重试或上传文件", 503)
            await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))


def source_bucket(url):
    # Validate configuration before taking a permit; never put proxy secrets in keys.
    server_proxy(url)
    platform = platform_for(url)
    if platform == "other":
        from urllib.parse import urlsplit
        platform += "-" + hashlib.sha256((urlsplit(url).hostname or "").encode()).hexdigest()[:16]
    return f"{platform}:{settings.VIDEO_EGRESS_POLICY_VERSION}"


@asynccontextmanager
async def source_operation(url, *, timeout=30, cancelled=None, metadata=False, success_outcome="success"):
    now = time.monotonic()
    deadline = now + timeout
    if metadata and _metadata_deadline.get() is not None:
        deadline = min(deadline, _metadata_deadline.get())
    if settings.VIDEO_EGRESS_MODE == "legacy":
        yield SourceLease(deadline, min(deadline, now + 30))
        return
    bucket = source_bucket(url)
    current = _active.get()
    if current is not None and current.bucket == bucket:
        yield current
        return
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1,
                           socket_timeout=1, retry=Retry(NoBackoff(), 0))
    try:
        coordinator = SourceCoordinator(redis)
        lease = await coordinator.acquire(bucket, deadline, cancelled)
        token = _active.set(lease)
        try:
            yield lease
        except BaseException as error:
            blocked = getattr(error, "source_failure", None) in {"restricted", "rate_limited"}
            outcome = "failure" if blocked else "neutral"
            try:
                await coordinator.update(lease, "finish", outcome)
            except AppException:
                pass  # Preserve the original failure/cancellation; Redis permits expire.
            raise
        else:
            await coordinator.update(lease, "finish", success_outcome)
        finally:
            _active.reset(token)
    finally:
        await redis.aclose()


async def prepare_source_link(value):
    url = await validate_url(canonical_link(value))
    if settings.VIDEO_EGRESS_MODE == "platform" and is_domestic_short_link(url):
        async with source_operation(url, metadata=True, success_outcome="neutral") as lease:
            try:
                async with asyncio.timeout(lease.metadata_remaining):
                    url = await resolve_short_link(url)
            except TimeoutError:
                raise AppException("PARSE_TIMEOUT", "短链还原超过执行时限", 504) from None
    return url
