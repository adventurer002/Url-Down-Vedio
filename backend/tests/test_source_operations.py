import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from test_tasks import redis_url
from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.main import create_app
from app.modules.download.adapter import YtDlpDownloadAdapter
from app.modules.transcript.subtitles import YtDlpSubtitleProvider
from app.modules.video.parsers import cached, operations
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.router import get_video_service
from app.modules.video.service import VideoService

URL = "https://www.bilibili.com/video/BVabc"


@pytest.fixture
async def coordinated(redis_url, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    monkeypatch.setattr(settings, "VIDEO_EGRESS_POLICY_VERSION", "test_" + uuid4().hex[:16])
    client = Redis.from_url(redis_url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1)
    coordinator = operations.SourceCoordinator(client)
    bucket = operations.source_bucket(URL)
    try:
        yield coordinator, bucket
    finally:
        keys = [key async for key in client.scan_iter(match=f"videohub:source:v1:*:{settings.VIDEO_EGRESS_POLICY_VERSION}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def take(coordinator, bucket, timeout=2):
    return await coordinator.acquire(bucket, time.monotonic() + timeout)


async def block(coordinator, bucket):
    for _ in range(settings.SOURCE_FAILURE_THRESHOLD):
        lease = await take(coordinator, bucket)
        await coordinator.update(lease, "finish", "failure")
    return lease


async def test_failure_reports_idempotent_and_platforms_are_isolated(coordinated):
    coordinator, bucket = coordinated
    first = await take(coordinator, bucket)
    await coordinator.update(first, "finish", "failure")
    await coordinator.update(first, "finish", "failure")
    assert await coordinator.redis.hget(coordinator.keys(first)[0], "failures") == "1"
    for _ in range(settings.SOURCE_FAILURE_THRESHOLD - 1):
        lease = await take(coordinator, bucket)
        await coordinator.update(lease, "finish", "failure")
    with pytest.raises(AppException) as error:
        await take(coordinator, bucket)
    assert error.value.code == "SOURCE_COOLDOWN"
    domestic_other = await take(coordinator, bucket.replace("bilibili", "douyin"))
    await coordinator.update(domestic_other, "finish", "success")


async def test_success_resets_but_neutral_does_not_reset_source_failures(coordinated):
    coordinator, bucket = coordinated
    first = await take(coordinator, bucket)
    await coordinator.update(first, "finish", "failure")
    neutral = await take(coordinator, bucket)
    await coordinator.update(neutral, "finish", "neutral")
    state = coordinator.keys(first)[0]
    assert await coordinator.redis.hget(state, "failures") == "1"
    success = await take(coordinator, bucket)
    await coordinator.update(success, "finish", "success")
    assert await coordinator.redis.hget(state, "failures") == "0"


async def test_metadata_permit_is_shared_but_released_before_long_transfer(coordinated):
    coordinator, bucket = coordinated
    second_process = operations.SourceCoordinator(coordinator.redis)
    media = await take(coordinator, bucket)
    with pytest.raises(AppException) as error:
        await take(second_process, bucket, timeout=.02)
    assert error.value.code == "PARSE_BUSY"
    await media.metadata_ready()
    metadata = await take(second_process, bucket)
    await second_process.update(metadata, "finish", "success")
    await coordinator.update(media, "finish", "success")


async def test_single_half_open_probe_and_stale_success_cannot_close_new_circuit(coordinated):
    coordinator, bucket = coordinated
    old_media = await take(coordinator, bucket)
    await old_media.metadata_ready()
    last = await block(coordinator, bucket)
    await coordinator.update(old_media, "finish", "success")
    state = coordinator.keys(last)[0]
    assert int(await coordinator.redis.hget(state, "open_until")) > 0
    await coordinator.redis.hset(state, "open_until", 1)  # Advance state, not the machine clock.
    probe = await take(coordinator, bucket)
    with pytest.raises(AppException) as error:
        await take(coordinator, bucket)
    assert error.value.code == "SOURCE_COOLDOWN"
    await probe.metadata_ready()
    assert await coordinator.redis.hget(state, "open_until") == "0"
    normal = await take(coordinator, bucket)
    await coordinator.update(normal, "finish", "success")
    await coordinator.update(probe, "finish", "success")


async def test_cancelled_half_open_probe_is_recoverable(coordinated):
    coordinator, bucket = coordinated
    old = await block(coordinator, bucket)
    state = coordinator.keys(old)[0]
    await coordinator.redis.hset(state, "open_until", 1)
    probe = await take(coordinator, bucket)
    await coordinator.update(probe, "finish", "neutral")
    assert not await coordinator.redis.hget(state, "probe")
    following = await take(coordinator, bucket)
    assert following.generation > probe.generation
    await coordinator.update(following, "finish", "success")


async def test_expired_probe_fences_its_late_failure(coordinated):
    coordinator, bucket = coordinated
    old = await block(coordinator, bucket)
    state = coordinator.keys(old)[0]
    await coordinator.redis.hset(state, "open_until", 1)
    expired = await take(coordinator, bucket)
    await coordinator.redis.delete(coordinator.keys(expired)[1])
    await coordinator.redis.hset(state, "probe_until", 1)
    fresh = await take(coordinator, bucket)
    await coordinator.update(fresh, "finish", "success")
    await coordinator.update(expired, "finish", "failure")
    assert await coordinator.redis.hget(state, "open_until") == "0"


async def test_cancellation_while_waiting_does_not_steal_another_permit(coordinated):
    coordinator, bucket = coordinated
    owner = await take(coordinator, bucket)
    with pytest.raises(AppException) as error:
        await coordinator.acquire(bucket, time.monotonic() + 1, cancelled=AsyncMock(return_value=True))
    assert error.value.code == "TASK_CANCELLED"
    assert await coordinator.redis.get(coordinator.keys(owner)[1]) == owner.token
    await coordinator.update(owner, "finish", "neutral")


async def test_redis_failure_fails_closed_and_never_leaks_connection_details():
    client = SimpleNamespace(eval=AsyncMock(side_effect=RedisConnectionError("redis://operator:secret@private")))
    coordinator = operations.SourceCoordinator(client)
    with pytest.raises(AppException) as error:
        await take(coordinator, "bilibili:test")
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert "secret" not in error.value.message and "private" not in error.value.message


async def test_generic_403_does_not_trip_shared_circuit(coordinated):
    coordinator, bucket = coordinated
    for _ in range(4):
        with pytest.raises(AppException):
            async with operations.source_operation(URL):
                raise AppException("SOURCE_ACCESS_RESTRICTED", "source 403", 502)
    owner = await take(coordinator, bucket)
    assert await coordinator.redis.hget(coordinator.keys(owner)[0], "failures") != "3"
    await coordinator.update(owner, "finish", "neutral")


async def test_http_failures_stop_worker_download_and_subtitle_before_network(coordinated, monkeypatch, tmp_path):
    monkeypatch.setattr(cached, "validate_url", AsyncMock(side_effect=lambda url: url))
    import app.modules.video.service as video_service_module
    monkeypatch.setattr(video_service_module, "validate_url", AsyncMock(side_effect=lambda url: url))
    failure = AppException("SOURCE_ACCESS_RESTRICTED", "source blocked", 502)
    failure.source_failure = "restricted"
    raw = SimpleNamespace(parse=AsyncMock(side_effect=failure))
    parser = cached.CachedVideoParser(raw, cache=cached.MetadataCache(0, 1, failure_cooldown=0))
    app = create_app()
    app.dependency_overrides[get_video_service] = lambda: VideoService(parser)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(settings.SOURCE_FAILURE_THRESHOLD):
            response = await client.post("/api/v1/videos/parse", json={"url": URL})
            assert response.status_code == 502
        response = await client.post("/api/v1/videos/parse", json={"url": URL})
        assert response.status_code == 503 and response.json()["error"]["code"] == "SOURCE_COOLDOWN"
        assert response.json()["error"]["request_id"].startswith("req_")
    runner = SimpleNamespace(timeout=1, run=AsyncMock())
    controls = {"is_cancel_requested": AsyncMock(return_value=False), "on_progress": AsyncMock()}
    payload = {"url": URL, "preset_id": "audio_mp3"}
    for adapter in (YtDlpDownloadAdapter(runner), YtDlpSubtitleProvider(runner)):
        method = getattr(adapter, "download", None) or adapter.fetch
        with pytest.raises(AppException) as error:
            await method(payload, tmp_path, **controls)
        assert error.value.code == "SOURCE_COOLDOWN"
    runner.run.assert_not_awaited()
    assert raw.parse.await_count == settings.SOURCE_FAILURE_THRESHOLD


async def test_worker_failure_is_seen_by_another_api_process(coordinated, monkeypatch, tmp_path):
    _, bucket = coordinated
    failure = AppException("SOURCE_RATE_LIMITED", "limited", 503)
    failure.source_failure = "rate_limited"
    runner = SimpleNamespace(timeout=2, run=AsyncMock(side_effect=failure))
    controls = {"is_cancel_requested": AsyncMock(return_value=False), "on_progress": AsyncMock()}
    for _ in range(settings.SOURCE_FAILURE_THRESHOLD):
        with pytest.raises(AppException):
            await YtDlpDownloadAdapter(runner).download({"url": URL, "preset_id": "audio_mp3"}, tmp_path, **controls)
    monkeypatch.setattr(cached, "validate_url", AsyncMock(side_effect=lambda url: url))
    parser = SimpleNamespace(parse=AsyncMock())
    with pytest.raises(AppException) as error:
        await cached.CachedVideoParser(parser, cache=cached.MetadataCache(0, 1, failure_cooldown=0)).parse(URL)
    assert error.value.code == "SOURCE_COOLDOWN"
    parser.parse.assert_not_awaited()


async def test_state_is_shared_across_real_python_processes(coordinated):
    coordinator, bucket = coordinated
    program = """
import asyncio
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers.operations import source_operation
async def main():
    for _ in range(settings.SOURCE_FAILURE_THRESHOLD):
        try:
            async with source_operation('https://www.bilibili.com/video/BVabc'):
                error=AppException('SOURCE_RATE_LIMITED','limited',503)
                error.source_failure='rate_limited'
                raise error
        except AppException:
            pass
asyncio.run(main())
print('done')
"""
    environment = {**os.environ, "VIDEO_EGRESS_MODE": "platform", "REDIS_URL": settings.REDIS_URL,
                   "VIDEO_EGRESS_POLICY_VERSION": settings.VIDEO_EGRESS_POLICY_VERSION,
                   "SOURCE_FAILURE_THRESHOLD": str(settings.SOURCE_FAILURE_THRESHOLD)}
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True,
        timeout=10, start_new_session=True)
    assert result.returncode == 0 and result.stdout.strip() == "done"
    with pytest.raises(AppException) as error:
        await take(coordinator, bucket)
    assert error.value.code == "SOURCE_COOLDOWN"


async def test_cached_metadata_is_readable_during_cooldown_without_network(coordinated, monkeypatch):
    coordinator, bucket = coordinated
    monkeypatch.setattr(cached, "validate_url", AsyncMock(side_effect=lambda url: url))
    raw = SimpleNamespace(parse=AsyncMock(return_value=ParsedVideo("bilibili", "BVabc", "cached")))
    cache = cached.MetadataCache(120, 1, failure_cooldown=0)
    parser = cached.CachedVideoParser(raw, cache=cache)
    await parser.parse(URL)
    await block(coordinator, bucket)
    assert (await parser.parse(URL)).title == "cached"
    raw.parse.assert_awaited_once()


async def test_private_source_ready_event_releases_permit_without_becoming_sse(tmp_path):
    progress, ready = AsyncMock(), AsyncMock()
    program = "import json; print(json.dumps({'_source_metadata_ready':True})); print(json.dumps({'progress':10})); print(json.dumps({'output':'done'}))"
    runner = ProcessRunner(3, 1024*1024, interval=.01, term_grace=1)
    result = await runner.run([sys.executable, "-c", program], tmp_path,
        cwd=Path(__file__).resolve().parents[1], is_cancel_requested=AsyncMock(return_value=False),
        on_progress=progress, on_source_ready=ready)
    ready.assert_awaited_once()
    assert result == {"output": "done"}
    assert [call.args[0] for call in progress.await_args_list] == [{"progress": 10}]


async def test_stalled_source_metadata_is_stopped_before_transfer_timeout(tmp_path):
    runner = ProcessRunner(3, 1024*1024, interval=.01, term_grace=1)
    with pytest.raises(AppException) as error:
        await runner.run([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path,
            cwd=Path(__file__).resolve().parents[1], is_cancel_requested=AsyncMock(return_value=False),
            on_progress=AsyncMock(), on_source_ready=AsyncMock(), source_metadata_timeout=.05)
    assert error.value.code == "PARSE_TIMEOUT" and runner.directory_stopped(tmp_path)


@pytest.mark.parametrize("kind", ["download", "subtitle"])
async def test_platform_adapters_complete_and_release_metadata_lease(coordinated, tmp_path, kind):
    coordinator, bucket = coordinated
    seen = []

    class FixtureRunner(ProcessRunner):
        async def run(self, command, directory, **controls):
            seen.append(json.loads(command[3]))
            ready = controls["on_source_ready"]
            assert 0 < controls["source_metadata_timeout"] <= self.timeout

            async def verify_release():
                await ready()
                # A second API/Worker operation can start while this transfer runs.
                following = await take(coordinator, bucket)
                await coordinator.update(following, "finish", "neutral")

            controls["on_source_ready"] = verify_release
            filename = "output.mp3" if kind == "download" else "subtitle.en.srt"
            content = "fixture audio" if kind == "download" else "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
            result = {"output": filename} if kind == "download" else {"filename": filename, "language": "en"}
            program = ("import json; from pathlib import Path; "
                       "print(json.dumps({'_source_metadata_ready':True}), flush=True); "
                       f"Path({str(directory / filename)!r}).write_text({content!r}); "
                       f"print(json.dumps({result!r}), flush=True)")
            return await super().run([sys.executable, "-c", program], directory, **controls)

    runner = FixtureRunner(3, 1024 * 1024, interval=.01, term_grace=1)
    payload = {"url": URL + "?utm_source=share", "preset_id": "audio_mp3"}
    progress = AsyncMock()
    controls = {"is_cancel_requested": AsyncMock(return_value=False), "on_progress": progress}
    if kind == "download":
        result = await YtDlpDownloadAdapter(runner).download(payload, tmp_path, **controls)
        assert result.name == "output.mp3" and result.read_text() == "fixture audio"
    else:
        result = await YtDlpSubtitleProvider(runner).fetch(payload, tmp_path, **controls)
        assert result.language == "en" and result.content_text == "hello"
    assert seen == [{**payload, "url": URL}]
    progress.assert_not_awaited()
    assert runner.directory_stopped(tmp_path)


async def test_resolver_persists_resolved_short_link_and_reuses_identity(coordinated, monkeypatch):
    from app.modules.video.resolver import VideoResolver

    canonical = URL + "?p=2"
    monkeypatch.setattr(operations, "validate_url", AsyncMock(side_effect=lambda url: url))
    redirect = AsyncMock(return_value=canonical)
    monkeypatch.setattr(operations, "resolve_short_link", redirect)
    video = ParsedVideo("bilibili", "BVabc_p2", "part two")
    parser = SimpleNamespace(parse=AsyncMock(return_value=video))
    repository = SimpleNamespace(find_by_url=Mock(return_value=None), get_or_create=Mock(return_value=uuid4()))
    resolver = VideoResolver(parser, repository)
    resolved = await resolver.resolve("分享 https://b23.tv/abc 。")
    assert resolved["url"] == canonical and resolved["platform_video_id"] == "BVabc_p2"
    repository.get_or_create.assert_called_once_with(video, canonical)
    repository.find_by_url.assert_called_once_with(canonical)
    repository.find_by_url.return_value = resolved
    assert await resolver.resolve(canonical) == resolved
    parser.parse.assert_awaited_once_with(canonical)
    redirect.assert_awaited_once_with("https://b23.tv/abc")
