"""Short-lived API metadata cache; actual download/subtitle extraction stays fresh.

One cache per API event loop, at most 8 MiB/64 entries and 32 distinct in-flight
URLs. Identical requests share extraction; the last cancelled waiter stops it.
"""
import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import time
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import validate_source_url as validate_url
from app.infrastructure.timing import stage_timer
from app.modules.video.parsers.base import ParsedVideo, VideoParser
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.parsers.links import canonical_link
from app.modules.video.parsers.operations import metadata_budget, source_operation

logger = logging.getLogger(__name__)


@dataclass
class Flight:
    task: asyncio.Task
    waiters: int = 0
    retiring: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class MetadataCache:
    def __init__(self, ttl: float, concurrency: int, *, max_bytes=8 * 1024 * 1024, max_entries=64, max_pending=32,
                 failure_cooldown: float = 30):
        self.ttl, self.max_bytes, self.max_entries, self.max_pending = ttl, max_bytes, max_entries, max_pending
        self.entries = OrderedDict()
        self.bytes = 0
        self.flights: dict[str, Flight] = {}
        self.slots = asyncio.Semaphore(concurrency)
        self.failure_cooldown = failure_cooldown
        # Host-wide backoff prevents changing video IDs from hammering a blocked
        # platform. Store only sanitized fields, never exceptions/tracebacks.
        self.failures = OrderedDict()

    def check_cooldown(self, host):
        now = time.monotonic()
        for candidate, failure in list(self.failures.items()):
            if failure[0] <= now:
                self.failures.pop(candidate)
        failure = self.failures.get(host)
        if failure:
            raise AppException(*failure[1:])

    def remember_failure(self, host, error):
        if not self.failure_cooldown or error.code not in {"SOURCE_ACCESS_RESTRICTED", "SOURCE_RATE_LIMITED"}:
            return
        self.failures.pop(host, None)
        while len(self.failures) >= 64:
            self.failures.popitem(last=False)
        self.failures[host] = (time.monotonic() + self.failure_cooldown,
                               error.code, error.message, error.status_code)

    def discard(self, key):
        _, blob = self.entries.pop(key)
        self.bytes -= len(blob)

    def get(self, key):
        now = time.monotonic()
        for candidate, (expires, _) in list(self.entries.items()):
            if expires <= now:
                self.discard(candidate)
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key][1]
        return None

    def put(self, key, blob):
        if not self.ttl or len(blob) > self.max_bytes:
            return
        if key in self.entries:
            self.discard(key)
        while self.entries and (len(self.entries) >= self.max_entries or self.bytes + len(blob) > self.max_bytes):
            self.discard(next(iter(self.entries)))
        self.entries[key] = (time.monotonic() + self.ttl, blob)
        self.bytes += len(blob)

    async def parse(self, url: str, parser: VideoParser) -> ParsedVideo:
        blob = self.get(url)
        if blob is not None:
            logger.info("performance stage=metadata_cache outcome=hit")
            return ParsedVideo(**json.loads(blob))
        host = urlsplit(url).hostname
        self.check_cooldown(host)
        flight = self.flights.get(url)
        if flight is not None and flight.retiring:
            await flight.closed.wait()
            return await self.parse(url, parser)
        if flight is None:
            if len(self.flights) >= self.max_pending:
                raise AppException("PARSE_BUSY", "视频解析繁忙，请稍后重试", 503)

            async def extract():
                async with self.slots:
                    # A request may have queued before another URL was blocked.
                    self.check_cooldown(host)
                    try:
                        async with source_operation(url, timeout=30, metadata=True):
                            with stage_timer("metadata_parse"):
                                video = await parser.parse(url)
                    except AppException as error:
                        self.remember_failure(host, error)
                        raise
                    value = json.dumps(asdict(video), ensure_ascii=False).encode("utf-8")
                    self.put(url, value)
                    return value

            flight = Flight(asyncio.create_task(extract()))
            self.flights[url] = flight
            logger.info("performance stage=metadata_cache outcome=miss")
        else:
            logger.info("performance stage=metadata_cache outcome=shared")
        flight.waiters += 1
        try:
            blob = await asyncio.shield(flight.task)
            return ParsedVideo(**json.loads(blob))
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                flight.retiring = True
                # Keep the entry during cleanup: a new request must not overlap a
                # cancelled yt-dlp process before its process group has exited.
                if not flight.task.done():
                    flight.task.cancel()
                try:
                    await asyncio.gather(flight.task, return_exceptions=True)
                finally:
                    if self.flights.get(url) is flight:
                        self.flights.pop(url)
                    flight.closed.set()


def loop_cache() -> MetadataCache:
    loop = asyncio.get_running_loop()
    signature = (settings.VIDEO_PARSE_CACHE_TTL_SECONDS, settings.VIDEO_PARSE_CONCURRENCY,
                 hashlib.sha256(settings.YTDLP_PROXY.get_secret_value().encode()).digest(),
                 settings.YTDLP_JS_RUNTIME, settings.YTDLP_DENO_PATH,
                 settings.VIDEO_PARSE_FAILURE_COOLDOWN_SECONDS, settings.VIDEO_EGRESS_MODE,
                 hashlib.sha256(settings.YOUTUBE_PROXY.get_secret_value().encode()).digest(),
                 settings.VIDEO_EGRESS_POLICY_VERSION, settings.YOUTUBE_DNS_MODE)
    signature += (settings.DOUYIN_SOURCE_BACKEND, settings.DOUYIN_BROWSER_EXECUTABLE_PATH,
                  settings.DOUYIN_BROWSER_LIBRARY_PATH)
    current = getattr(loop, "_videohub_metadata_cache", None)
    if current is None or current[0] != signature:
        cooldown = settings.VIDEO_PARSE_FAILURE_COOLDOWN_SECONDS if settings.VIDEO_EGRESS_MODE == "legacy" else 0
        current = (signature, MetadataCache(signature[0], signature[1], failure_cooldown=cooldown))
        # The loop owns its state; no global references retain closed Celery/test loops.
        loop._videohub_metadata_cache = current
    return current[1]


class CachedVideoParser:
    def __init__(self, parser: VideoParser | None = None, *, cache: MetadataCache | None = None, timeout: float = 35):
        self.parser = parser or YtDlpParser()
        self.cache, self.timeout = cache, timeout

    async def parse(self, url: str) -> ParsedVideo:
        try:
            with metadata_budget(self.timeout):
                async with asyncio.timeout(self.timeout):
                    # Revalidate DNS even on a hit, without caching the security decision.
                    normalized = await validate_url(canonical_link(url))
                    return await (self.cache or loop_cache()).parse(normalized, self.parser)
        except TimeoutError:
            raise AppException("PARSE_TIMEOUT", "视频解析或排队超过总执行时限", 504) from None


def get_video_parser() -> VideoParser:
    return CachedVideoParser()
