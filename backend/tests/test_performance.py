import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.concurrency import ordered_map
from app.infrastructure.timing import stage_timer
from app.modules.ai.providers.base import LLMRequest, LLMResult
from app.modules.ai.providers.bounded import BoundedLLMProvider
from app.modules.ai.providers.heuristic import HeuristicLLMProvider
from app.modules.ai.providers.openai_compatible import OpenAICompatibleLLMProvider
from app.modules.ai.summarizer import Summarizer
from app.modules.transcript import providers as asr
from app.modules.transcript.data import make_transcript
from app.modules.video.parsers import cached
from app.modules.video.parsers.base import ParsedVideo


REQUEST = LLMRequest("map", {}, {}, "zh", "subtitle")
VIDEO = ParsedVideo("test", "123", "title", formats=[{"height": 720}])


async def test_summary_parallelism_preserves_output_usage_and_monotonic_progress():
    base = HeuristicLLMProvider()
    active = peak = 0

    async def reply(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            # Odd chapters finish first; merging must still follow source order.
            segments = request.context.get("segments", [{"start": 0}])
            await asyncio.sleep(.002 if segments[0]["start"] % 2 else .01)
            return await base.generate(request)
        finally:
            active -= 1

    transcript = SimpleNamespace(source_type="subtitle", content_text="unused",
        segments=[{"start": i, "end": i + 1, "text": str(i) + "证据" * 250} for i in range(8)])
    sequential = await Summarizer(base, chunk_chars=512, concurrency=1).summarize(transcript, "zh")
    progress = []

    async def advance(value):
        await asyncio.sleep(0)
        progress.append(value)

    parallel = await Summarizer(SimpleNamespace(generate=reply), chunk_chars=512, concurrency=2).summarize(
        transcript, "zh", on_progress=advance)
    assert parallel == sequential
    assert peak == 2 and active == 0
    assert progress == sorted(progress)


@pytest.mark.parametrize("failure", ["error", "cancel", "timeout"])
async def test_ordered_map_stops_siblings_before_returning(failure):
    both_started, stopped = asyncio.Event(), []
    active = 0

    async def operation(index):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        try:
            await both_started.wait()
            if index == 0 and failure == "error":
                raise AppException("UPSTREAM_FAILED", "failed", 502)
            await asyncio.Event().wait()
        finally:
            stopped.append(index)
            active -= 1

    task = asyncio.create_task(ordered_map(list(range(10)), operation, 2))
    await asyncio.wait_for(both_started.wait(), 1)
    if failure == "cancel":
        task.cancel()
    with pytest.raises({"error": AppException, "cancel": asyncio.CancelledError, "timeout": TimeoutError}[failure]):
        await asyncio.wait_for(task, .05)
    assert sorted(stopped) == [0, 1]
    assert active == 0


async def test_parallel_budget_reserves_and_waits_without_mutating_provider():
    release, started = asyncio.Event(), asyncio.Event()
    requests = []

    async def reply(request):
        requests.append(request)
        if len(requests) == 2:
            started.set()
        await release.wait()
        return LLMResult({}, 1, 0, 1, "test")

    raw = SimpleNamespace(name="test", model="test", max_tokens=256, generate=reply)
    provider = BoundedLLMProvider(raw, {"total_output_tokens": 512, "output_tokens_per_call": 256, "max_calls": 3})
    tasks = [asyncio.create_task(provider.generate(REQUEST)) for _ in range(3)]
    await asyncio.wait_for(started.wait(), 1)
    assert len(requests) == 2 and provider.reserved_tokens == 512
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert len(requests) == 3 and all(request.max_output_tokens == 256 for request in requests)
    assert provider.output_tokens == provider.reserved_tokens == 0
    assert raw.max_tokens == 256 and REQUEST.max_output_tokens is None
    with pytest.raises(AppException, match="规模上限"):
        await provider.generate(REQUEST)


@pytest.mark.parametrize("outcome", ["unknown", "failure", "cancel"])
async def test_budget_unknown_usage_consumes_reservation_and_does_not_deadlock(outcome):
    started = asyncio.Event()

    async def reply(request):
        started.set()
        if outcome == "failure":
            raise AppException("UPSTREAM_FAILED", "failed", 502)
        if outcome == "cancel":
            await asyncio.Event().wait()
        return LLMResult({}, None, None, 1, "test")

    provider = BoundedLLMProvider(SimpleNamespace(name="test", model="test", generate=reply),
        {"total_output_tokens": 256, "output_tokens_per_call": 256, "max_calls": 3})
    task = asyncio.create_task(provider.generate(REQUEST))
    await asyncio.wait_for(started.wait(), 1)
    if outcome == "cancel":
        task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    assert (isinstance(results[0], BaseException)) == (outcome != "unknown")
    assert provider.reserved_tokens == 0 and provider.output_tokens == 256
    with pytest.raises(AppException, match="规模上限"):
        await asyncio.wait_for(provider.generate(REQUEST), 1)


async def test_http_provider_honors_request_token_reservation():
    def respond(request):
        assert json.loads(request.content)["max_tokens"] == 256
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 128}})
    provider = OpenAICompatibleLLMProvider("https://example.com", "test", "test", max_tokens=4096,
                                          transport=httpx.MockTransport(respond))
    bounded = BoundedLLMProvider(provider, {"total_output_tokens": 256, "output_tokens_per_call": 4096, "max_calls": 2})
    await bounded.generate(REQUEST)
    assert provider.max_tokens == 4096 and bounded.output_tokens == 128


@pytest.fixture
def public_dns(monkeypatch):
    validator = AsyncMock(side_effect=lambda url: url)
    monkeypatch.setattr(cached, "validate_url", validator)
    return validator


async def test_metadata_singleflight_copy_isolation_and_dns_revalidation(public_dns):
    release, started = asyncio.Event(), asyncio.Event()

    async def extract(url):
        started.set()
        await release.wait()
        return VIDEO

    raw = SimpleNamespace(parse=AsyncMock(side_effect=extract))
    cache = cached.MetadataCache(120, 2)
    parser = cached.CachedVideoParser(raw, cache=cache)
    tasks = [asyncio.create_task(parser.parse("https://example.com/video")) for _ in range(3)]
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    first, second, third = await asyncio.gather(*tasks)
    first.formats[0]["height"] = 1080
    assert second.formats[0]["height"] == third.formats[0]["height"] == 720
    assert (await parser.parse("https://example.com/video")).formats[0]["height"] == 720
    raw.parse.assert_awaited_once()
    assert public_dns.await_count == 4
    public_dns.side_effect = AppException("SSRF_BLOCKED", "private DNS", 400)
    with pytest.raises(AppException, match="private DNS"):
        await parser.parse("https://example.com/video")
    assert not cache.flights


@pytest.mark.parametrize("cancel_all", [False, True])
async def test_metadata_cancelled_waiter_does_not_leak_or_cancel_other_waiters(public_dns, cancel_all):
    release, started, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def extract(url):
        started.set()
        try:
            await release.wait()
            return VIDEO
        finally:
            cleaned.set()

    cache = cached.MetadataCache(120, 2)
    parser = cached.CachedVideoParser(SimpleNamespace(parse=extract), cache=cache)
    first = asyncio.create_task(parser.parse("https://example.com/video"))
    second = asyncio.create_task(parser.parse("https://example.com/video"))
    await asyncio.wait_for(started.wait(), 1)
    assert cache.flights["https://example.com/video"].waiters == 2
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not cleaned.is_set()
    if cancel_all:
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not cache.entries
    else:
        release.set()
        assert (await second).platform_video_id == "123"
    assert cleaned.is_set() and not cache.flights


async def test_metadata_failure_not_cached_and_timeout_cleans_up(public_dns):
    raw = SimpleNamespace(parse=AsyncMock(side_effect=[AppException("PARSE_FAILED", "failed", 502), VIDEO]))
    cache = cached.MetadataCache(120, 1)
    parser = cached.CachedVideoParser(raw, cache=cache)
    with pytest.raises(AppException):
        await parser.parse("https://example.com/video")
    assert not cache.entries and not cache.flights
    await parser.parse("https://example.com/video")
    assert raw.parse.await_count == 2
    stopped = asyncio.Event()

    async def stuck(url):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    parser = cached.CachedVideoParser(SimpleNamespace(parse=stuck), cache=cache, timeout=.02)
    with pytest.raises(AppException) as error:
        await parser.parse("https://example.com/other")
    assert error.value.code == "PARSE_TIMEOUT" and stopped.is_set() and not cache.flights


@pytest.mark.parametrize("code", ["SOURCE_ACCESS_RESTRICTED", "SOURCE_RATE_LIMITED"])
async def test_platform_cooldown_blocks_other_urls_but_revalidates_dns(public_dns, monkeypatch, code):
    now = 0
    public_resolution = public_dns.side_effect
    monkeypatch.setattr(cached.time, "monotonic", lambda: now)
    raw = SimpleNamespace(parse=AsyncMock(side_effect=[AppException(code, "平台暂时限制访问", 502), VIDEO, VIDEO]))
    cache = cached.MetadataCache(120, 1, failure_cooldown=30)
    parser = cached.CachedVideoParser(raw, cache=cache)
    for url in ["https://example.com/first", "https://example.com/second"]:
        with pytest.raises(AppException) as error:
            await parser.parse(url)
        assert error.value.code == code
    assert raw.parse.await_count == 1
    await parser.parse("https://other.example/video")
    assert raw.parse.await_count == 2
    public_dns.side_effect = AppException("SSRF_BLOCKED", "private DNS", 400)
    with pytest.raises(AppException) as error:
        await parser.parse("https://example.com/second")
    assert error.value.code == "SSRF_BLOCKED"
    public_dns.side_effect = public_resolution
    now = 31
    await parser.parse("https://example.com/second")
    assert raw.parse.await_count == 3 and not cache.flights


async def test_queued_metadata_observes_new_platform_cooldown(public_dns):
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(url):
        started.set()
        await release.wait()
        raise AppException("SOURCE_ACCESS_RESTRICTED", "平台暂时限制访问", 502)

    raw = SimpleNamespace(parse=AsyncMock(side_effect=blocked))
    cache = cached.MetadataCache(120, 1)
    parser = cached.CachedVideoParser(raw, cache=cache)
    first = asyncio.create_task(parser.parse("https://example.com/first"))
    await started.wait()
    second = asyncio.create_task(parser.parse("https://example.com/second"))
    await asyncio.sleep(0)
    release.set()
    errors = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(error, AppException) and error.code == "SOURCE_ACCESS_RESTRICTED" for error in errors)
    assert raw.parse.await_count == 1 and not cache.flights


def test_platform_cooldown_is_bounded_and_can_be_disabled():
    cache = cached.MetadataCache(120, 1)
    error = AppException("SOURCE_RATE_LIMITED", "稍后重试", 503)
    for index in range(100):
        cache.remember_failure(str(index), error)
    assert len(cache.failures) == 64
    cache = cached.MetadataCache(120, 1, failure_cooldown=0)
    cache.remember_failure("example.com", error)
    assert not cache.failures


def test_metadata_ttl_entry_and_byte_limits(monkeypatch):
    now = 0
    monkeypatch.setattr(cached.time, "monotonic", lambda: now)
    cache = cached.MetadataCache(10, 1, max_bytes=6, max_entries=2)
    cache.put("a", b"123")
    cache.put("b", b"123")
    assert cache.get("a") == b"123"
    cache.put("c", b"123")
    assert cache.get("b") is None and cache.bytes == 6
    cache.put("large", b"1234567")
    assert "large" not in cache.entries
    now = 11
    assert cache.get("a") is None and cache.bytes == 0
    cache.ttl = 0
    cache.put("disabled", b"123")
    assert not cache.entries


async def test_metadata_limits_distinct_processes_and_pending_requests(public_dns):
    release, started = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def extract(url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
            return VIDEO
        finally:
            active -= 1

    cache = cached.MetadataCache(120, 2, max_pending=3)
    parser = cached.CachedVideoParser(SimpleNamespace(parse=extract), cache=cache)
    tasks = [asyncio.create_task(parser.parse(f"https://example.com/{index}")) for index in range(3)]
    await asyncio.wait_for(started.wait(), 1)
    with pytest.raises(AppException) as error:
        await parser.parse("https://example.com/full")
    assert error.value.code == "PARSE_BUSY"
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2 and active == 0 and not cache.flights


@pytest.mark.parametrize("failure", [False, True])
async def test_qwen_parallel_chunks_keep_timestamps_and_cleanup(tmp_path, monkeypatch, failure):
    source = tmp_path / "original.mp3"
    source.write_bytes(b"original")
    monkeypatch.setattr(settings, "ASR_CHUNK_CONCURRENCY", 2)
    monkeypatch.setattr(asr.shutil, "which", lambda _: "/opt/ffmpeg")

    async def split(runner, command, directory, **kwargs):
        for index in range(3):
            with wave.open(str(directory / f"chunk-{index:05d}.wav"), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * 16000 * (index + 1))
        return {}

    monkeypatch.setattr(asr.ProcessRunner, "run", split)
    active = peak = 0
    both_started = asyncio.Event()

    async def transcribe(path, language):
        nonlocal active, peak
        index = int(path.stem.split("-")[1])
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        try:
            await asyncio.wait_for(both_started.wait(), 1)
            if failure:
                if index == 0:
                    raise AppException("ASR_API_ERROR", "failed", 502)
                await asyncio.Event().wait()
            await asyncio.sleep(.02 if index == 0 else .001)
            return make_transcript("zh", [{"start": 0, "end": 1, "text": f"chunk {index}"}])
        finally:
            assert path.exists()  # Parent must wait for sibling cleanup before rmtree.
            active -= 1

    provider = asr.QwenASRProvider("https://example.com", "test", "qwen3-asr-flash")
    provider._transcribe_chunk = transcribe
    if failure:
        with pytest.raises(AppException):
            await asyncio.wait_for(provider.transcribe(source, None), 1)
    else:
        data = await provider.transcribe(source, None)
        assert [segment["start"] for segment in data.segments] == [0, 1, 3]
        assert [segment["end"] for segment in data.segments] == [1, 3, 6]
        assert [segment["text"] for segment in data.segments] == ["chunk 0", "chunk 1", "chunk 2"]
    assert peak == 2 and active == 0
    assert source.read_bytes() == b"original" and not list(tmp_path.glob("asr-chunks-*"))


def test_stage_timing_logs_failure_without_source_content(caplog, monkeypatch):
    # Earlier integration tests run Alembic fileConfig in-process, which disables
    # existing loggers. Production migrations and API/Worker run separately.
    from app.infrastructure import timing
    monkeypatch.setattr(timing.logger, "disabled", False)
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        with stage_timer("summary", "test-task"):
            raise RuntimeError("private transcript")
    assert "stage=summary task_id=test-task duration_ms=" in caplog.text
    assert "outcome=interrupted" in caplog.text and "private transcript" not in caplog.text


async def test_metadata_new_waiter_waits_for_cancelled_process_cleanup(public_dns):
    started, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def extract(url):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
        return VIDEO

    cache = cached.MetadataCache(120, 2)
    parser = cached.CachedVideoParser(SimpleNamespace(parse=extract), cache=cache)
    first = asyncio.create_task(parser.parse("https://example.com/video"))
    await asyncio.wait_for(started.wait(), 1)
    first.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    second = asyncio.create_task(parser.parse("https://example.com/video"))
    await asyncio.sleep(0)
    assert calls == 1
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await asyncio.wait_for(second, 1)).platform_video_id == "123"
    assert calls == 2 and not cache.flights
