from contextlib import contextmanager, nullcontext
import shutil
import subprocess
from threading import Barrier
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redis.asyncio import Redis
import pytest
from yt_dlp.utils import DownloadError

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.modules.download import child
from app.modules.download.adapter import LocalRenderAdapter, YtDlpDownloadAdapter
from app.modules.download.fast_mp4 import fast_start_layout
from app.modules.download.presets import select_streams
from app.modules.video.parsers import source_cache
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.parsers.bilibili import HEADERS
from test_download import storage
from test_tasks import redis_url


def snapshots(now):
    formats = [
        {"format_id": "video-32-avc1", "ext": "mp4", "protocol": "https",
         "url": f"https://edge.bilivideo.com/v.m4s?deadline={now + 50}&sig=private",
         "vcodec": "avc1", "acodec": "none", "height": 720, "http_headers": dict(HEADERS)},
        {"format_id": "audio-30280-mp4a", "ext": "m4a", "protocol": "https",
         "url": f"https://edge.bilivideo.com/a.m4s?deadline={now + 90}&sig=private",
         "vcodec": "none", "acodec": "mp4a", "http_headers": dict(HEADERS)},
    ]
    return ParsedVideo("bilibili", "BVFixture", "Test", duration=20, formats=formats)


def youtube_formats(fps=30):
    common = {
        "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none",
        "height": 720, "width": 1280, "fps": 30,
    }
    hls = {**common, "format_id": "232", "url": "https://media.example.com/playlist",
           "protocol": "m3u8_native", "tbr": 1072}
    progressive = {**common, "format_id": "136", "url": "https://media.example.com/video",
                   "protocol": "https", "tbr": 268, "fps": fps}
    audio = {"format_id": "140", "url": "https://media.example.com/audio",
             "protocol": "https", "vcodec": "none", "acodec": "mp4a", "ext": "m4a"}
    return hls, progressive, audio


@pytest.mark.parametrize("fps,expected_format", [(30, "136"), (24, "232")])
def test_youtube_prefers_equivalent_unfragmented_https(monkeypatch, fps, expected_format):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    hls, progressive, audio = youtube_formats(fps)
    video = ParsedVideo("youtube", "fixture", "Test", duration=643, formats=[hls, progressive, audio])
    assert [item["format_id"] for item in select_streams(video, "video_720_mp4")] == [expected_format, "140"]
    assert select_streams(video, "video_720_mp4", prefer_https=False) == [hls, audio]


def test_youtube_rejected_https_retries_original_manifest(monkeypatch, tmp_path):
    hls, progressive, audio = youtube_formats()
    info = {"id": "fixture", "extractor_key": "Youtube", "duration": 643,
            "formats": [hls, progressive, audio]}
    guard = SimpleNamespace(violation=None, installed=nullcontext, check_url=lambda url: None)
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(child, "server_proxy", lambda url: None)
    monkeypatch.setattr(child, "extraction_options", lambda proxy, *, url: {})
    monkeypatch.setattr(child.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(child, "NetworkGuard", lambda proxy: guard)
    monkeypatch.setattr(child, "guarded_downloader", lambda *args: nullcontext())
    monkeypatch.setattr(child, "extract_source", lambda *args: info)
    calls = []
    partial = tmp_path / "source_0.mp4.part"

    def transfer(guard, info, streams, options, directory, size, audio_only, emit):
        calls.append(streams)
        if len(calls) == 1:
            partial.write_bytes(b"partial")
            raise DownloadError("HTTP Error 403: Forbidden")
        assert not partial.exists()
        return [directory / "source_0.mp4", directory / "source_1.m4a"]

    monkeypatch.setattr(child, "transfer_streams", transfer)
    child.download({"url": "https://www.youtube.com/watch?v=fixture", "platform": "youtube",
                    "platform_video_id": "fixture", "preset_id": "video_720_mp4",
                    "_defer_render": True}, tmp_path, lambda event: None)
    assert calls == [[progressive, audio], [hls, audio]]


async def test_source_cache_ttl_scope_rejects_unknown_expiry_and_reuses(redis_url, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_SOURCE_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "REDIS_URL", redis_url)
    now = int(time.time())
    video = snapshots(now)
    payload = {"platform": video.platform, "platform_video_id": video.platform_video_id}
    async with Redis.from_url(redis_url) as redis:
        monkeypatch.setattr(source_cache, "redis_client", redis)
        await source_cache.remember(video)
        assert 0 < await redis.ttl(source_cache.key(video.platform_video_id)) <= 35
        raw = await source_cache.load(payload)
        assert source_cache.restore(raw, payload) == video
        assert await source_cache.load({**payload, "platform": "youtube"}) is None
        assert source_cache.restore(raw, {**payload, "platform_video_id": "another"}) is None
        video.formats[0]["http_headers"]["Cookie"] = "must-not-cache"
        assert source_cache.snapshot(video, now) is None
        video.formats[0]["http_headers"] = HEADERS.copy()
        video.formats[0]["url"] = "https://edge.bilivideo.com/v.m4s?sig=unknown-expiry"
        assert source_cache.snapshot(video, now) is None
        await redis.delete(source_cache.key(video.platform_video_id))


@pytest.mark.parametrize("error_code,expected_calls", [("DOWNLOAD_SOURCE_STALE", 2), ("SSRF_BLOCKED", 1)])
async def test_only_stale_sources_refresh_and_cache_is_not_in_argv(tmp_path, monkeypatch, error_code, expected_calls):
    monkeypatch.setattr(source_cache, "load", AsyncMock(return_value=b'{"private":"signature"}'))
    adapter = YtDlpDownloadAdapter(SimpleNamespace(timeout=2))
    calls = []

    async def execute(payload, directory, **controls):
        calls.append(payload)
        cache = directory / ".source-cache.json"
        if payload.get("_cached_source"):
            assert cache.stat().st_mode & 0o777 == 0o600 and "signature" not in str(payload)
            shutil.rmtree(directory)
            raise AppException(error_code, "fixture", 502)
        assert directory.is_dir() and not cache.exists()
        return directory / "output.mp4"

    monkeypatch.setattr(adapter, "_execute", execute)
    directory = tmp_path / "work"
    directory.mkdir()
    if expected_calls == 1:
        with pytest.raises(AppException) as error:
            await adapter.download({}, directory)
        assert error.value.code == error_code
    else:
        assert await adapter.download({}, directory) == directory / "output.mp4"
    assert len(calls) == expected_calls


@pytest.mark.parametrize("limit", [16, 8])
def test_parallel_streams_share_the_combined_size_cap(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(settings, "DOWNLOAD_PARALLEL_STREAMS_ENABLED", True)
    ready = Barrier(2)

    @contextmanager
    def downloader(guard, options, **kwargs):
        def transfer(info, download):
            ready.wait(timeout=2)
            for hook in options["progress_hooks"]:
                hook({"downloaded_bytes": 6, "total_bytes": 6, "status": "finished"})
            with open(options["outtmpl"], "wb") as output:
                output.write(b"stream")
        yield SimpleNamespace(process_ie_result=transfer)

    monkeypatch.setattr(child, "guarded_downloader", downloader)
    guard = SimpleNamespace(platform="bilibili", proxy_address=None, violation=None)
    streams = snapshots(int(time.time())).formats
    def run():
        return child.transfer_streams(guard, {}, streams, {}, tmp_path, limit, False, lambda event: None)
    if limit == 8:
        with pytest.raises(AppException) as caught:
            run()
        assert caught.value.code == "FILE_TOO_LARGE"
    else:
        assert [path.name for path in run()] == ["source_0.mp4", "source_1.m4a"]


@pytest.mark.parametrize("fast", [True, False])
async def test_mp4_reuse_preserves_bytes_and_late_moov_is_remuxed(tmp_path, monkeypatch, fast):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("requires FFmpeg and ffprobe")
    monkeypatch.setattr(settings, "DOWNLOAD_FAST_MP4_ENABLED", True)
    path = tmp_path / "source_0.mp4"
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=5:duration=1",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-threads", "1", "-c:a", "aac"]
    if fast:
        command.extend(["-movflags", "+faststart"])
    subprocess.run([*command, str(path)], check=True, timeout=10)
    original = path.read_bytes()
    assert fast_start_layout(path) == fast
    (tmp_path / "download-sources.json").write_text(
        '{"sources":["source_0.mp4"],"codecs":[{"vcodec":"avc1","acodec":"mp4a"}]}')
    runner = ProcessRunner(10, 10 * 1024**2)
    if fast:
        monkeypatch.setattr(runner, "run", AsyncMock(side_effect=AssertionError("compatible MP4 should skip remux")))
    rendered = await LocalRenderAdapter(runner).download({"preset_id": "video_720_mp4"}, tmp_path,
        is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
    assert fast_start_layout(rendered)
    assert (rendered.read_bytes() == original) == fast


def test_disk_guard_uses_global_scan_at_bounded_intervals(storage, monkeypatch):
    from app.infrastructure.storage import local
    now = 0.
    calls = []
    monkeypatch.setattr(local.time, "monotonic", lambda: now)
    monkeypatch.setattr(storage, "check_capacity", lambda: calls.append(now))
    check = storage.download_capacity_guard()
    for now in (.5, 1, 4.5, 5, 6, 10):
        check()
    assert calls == [5, 10]
