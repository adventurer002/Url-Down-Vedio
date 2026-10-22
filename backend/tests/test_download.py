import asyncio
from contextlib import nullcontext
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func, select, update

from test_tasks import MemoryBus, repository
from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.storage import LocalStorageAdapter
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.main import create_app
from app.modules.download import service as download_module
from app.modules.download.child import ffmpeg_command, native_network_only
from app.modules.download.presets import select_streams
from app.modules.download.router import get_download_service, get_media_service
from app.modules.download.service import DownloadService
from app.modules.download.worker import execute_download
from app.modules.media.models import MediaFile
from app.modules.media.repository import MediaRecord, MediaRepository
from app.modules.media.service import MediaService
from app.modules.tasks.models import Task
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import TaskService
from app.modules.video.models import Video
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.repository import VideoRepository


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(tmp_path / "storage", quota_mb=1024, min_free_mb=0)


@pytest.fixture
def video():
    return ParsedVideo("youtube", "fixture", "../视频\n样本", duration=1, formats=[
        {"format_id": "720", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4",
         "url": "https://example.com/source.mp4", "protocol": "https"},
    ])


async def make_task(repository, video):
    video_id = VideoRepository(repository.sessions).get_or_create(video, "https://example.com/watch")
    return repository.create("download", video_id, {"url": "https://example.com/watch", "preset_id": "video_720_mp4",
                                                   "platform": video.platform, "platform_video_id": video.platform_video_id,
                                                   "title": video.title})


class FixtureAdapter:
    async def download(self, payload, directory, **controls):
        await controls["on_progress"]({"progress": 90, "stage": "processing"})
        output = directory / "output.mp4"
        output.write_bytes(b"fixture-media-bytes")
        return output


async def test_create_download_202_and_reuse_video(repository, storage, video, monkeypatch):
    monkeypatch.setattr(download_module, "validate_url", AsyncMock(return_value="https://example.com/watch"))
    parser = Mock(parse=AsyncMock(return_value=video))
    dispatcher = Mock()
    service = DownloadService(parser, VideoRepository(repository.sessions),
                              TaskService(repository, MemoryBus(), dispatcher), storage)
    app = create_app()
    app.dependency_overrides[get_download_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(2):
            response = await client.post("/api/v1/downloads", json={"url": "https://example.com/watch", "preset_id": "video_720_mp4"})
            assert response.status_code == 202
            from uuid import UUID
            task = repository.get(UUID(response.json()["data"]["task_id"]))
            assert task.task_type == "download" and task.status == "queued"
        invalid = await client.post("/api/v1/downloads", json={"url": "https://example.com/watch", "preset_id": "video_999_mp4"})
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "INVALID_PRESET"
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Video)) == 1
    assert dispatcher.enqueue.call_count == 2
    assert not list((storage.root / "files").iterdir())


async def test_unsafe_url_never_queued(repository, storage, video):
    parser = Mock(parse=AsyncMock(return_value=video))
    dispatcher = Mock()
    service = DownloadService(parser, VideoRepository(repository.sessions), TaskService(repository, MemoryBus(), dispatcher), storage)
    with pytest.raises(AppException) as exc:
        await service.create("http://127.0.0.1/private", "video_720_mp4")
    assert exc.value.code == "SSRF_BLOCKED"
    parser.parse.assert_not_awaited()
    dispatcher.enqueue.assert_not_called()


@pytest.mark.parametrize("reason,ignore_term", [("cancel", False), ("timeout", True), ("size", False)])
async def test_watchdog_kills_entire_group_and_cleans_directory(tmp_path, reason, ignore_term):
    directory = tmp_path / "task"
    directory.mkdir()
    child_pid_file = tmp_path / "child.pid"
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time,signal; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + f"p=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        + f"Path({str(child_pid_file)!r}).write_text(str(p.pid)); "
        + f"Path({str(directory / 'partial')!r}).write_bytes(b'x'*{1000 if reason == 'size' else 1}); "
        + "time.sleep(60)"
    )
    started = time.monotonic()

    async def cancelled():
        return reason == "cancel" and child_pid_file.exists() and time.monotonic() - started > 0.15

    runner = ProcessRunner(timeout=0.3 if reason == "timeout" else 5, max_bytes=100 if reason == "size" else 10000,
                           interval=0.05, term_grace=0.2)
    with pytest.raises(AppException) as exc:
        await runner.run([sys.executable, "-c", parent_code], directory, cwd=tmp_path,
                         is_cancel_requested=cancelled, on_progress=AsyncMock())
    assert exc.value.code == {"cancel": "TASK_CANCELLED", "timeout": "DOWNLOAD_TIMEOUT", "size": "FILE_TOO_LARGE"}[reason]
    assert not directory.exists()
    child_pid = int(child_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


async def test_watchdog_success_consumes_progress(tmp_path):
    directory = tmp_path / "task"
    directory.mkdir()
    code = "import json; print(json.dumps({'progress': 90, 'stage': 'processing'}), flush=True)"
    progress = AsyncMock()
    result = await ProcessRunner(2, 1000, interval=0.05).run(
        [sys.executable, "-c", code], directory, cwd=tmp_path,
        is_cancel_requested=AsyncMock(return_value=False), on_progress=progress)
    assert result["progress"] == 90 and directory.exists()
    progress.assert_awaited_once()


async def test_complete_and_file_response_stream(repository, storage, video):
    task = await make_task(repository, video)
    media = MediaRepository(repository.sessions)
    result = await execute_download(task.id, TaskService(repository, MemoryBus()), media, storage, FixtureAdapter())
    assert result["status"] == "completed"
    stored = repository.get(task.id)
    assert stored.result_payload["download_url"].startswith("/api/v1/files/")
    assert "storage" not in json.dumps(stored.result_payload)
    app = create_app()
    app.dependency_overrides[get_media_service] = lambda: MediaService(media, storage)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(stored.result_payload["download_url"])
        assert response.status_code == 200 and response.content == b"fixture-media-bytes"
        assert response.headers["content-type"].startswith("video/mp4")
        assert str(storage.root) not in str(response.headers)
        assert "attachment" in response.headers["content-disposition"]
        for file_id in [uuid4(), stored.result_payload["file_id"]]:
            if str(file_id) == stored.result_payload["file_id"]:
                with repository.sessions.begin() as session:
                    session.execute(update(MediaFile).values(expires_at=utcnow() - timedelta(seconds=1)))
            missing = await client.get(f"/api/v1/files/{file_id}/download")
            assert missing.status_code == 404 and missing.json()["error"]["code"] == "FILE_NOT_FOUND"
            assert missing.json()["error"]["request_id"] == missing.headers["X-Request-ID"]


async def test_cancel_after_promotion_cas_discards_file(repository, storage, video, monkeypatch):
    task = await make_task(repository, video)
    media = MediaRepository(repository.sessions)
    original = storage.promote

    def cancel_then_promote(*args):
        key = original(*args)
        repository.request_cancel(task.id)
        return key

    monkeypatch.setattr(storage, "promote", cancel_then_promote)
    result = await execute_download(task.id, TaskService(repository, MemoryBus()), media, storage, FixtureAdapter())
    assert result["status"] == "cancelled"
    assert repository.get(task.id).error_code == "TASK_CANCELLED"
    assert not list((storage.root / "files").iterdir())
    assert not storage.task_directory(task.id).exists()
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MediaFile)) == 0


async def test_failed_registration_rolls_back_completion_and_cleans_files(repository, storage, video, monkeypatch):
    task = await make_task(repository, video)
    media = MediaRepository(repository.sessions)
    monkeypatch.setattr(media, "register", Mock(side_effect=RuntimeError("secret path")))
    result = await execute_download(task.id, TaskService(repository, MemoryBus()), media, storage, FixtureAdapter())
    assert result["status"] == "failed"
    assert repository.get(task.id).result_payload is None
    assert "secret" not in repository.get(task.id).error_message
    assert not list((storage.root / "files").iterdir())
    assert not storage.task_directory(task.id).exists()


def test_quota_free_space_and_path_traversal(tmp_path, monkeypatch):
    storage = LocalStorageAdapter(tmp_path / "store", quota_mb=1, min_free_mb=0)
    (storage.root / "files" / "filled").write_bytes(b"x" * 1024 * 1024)
    with pytest.raises(AppException) as exc:
        storage.check_capacity()
    assert exc.value.code == "STORAGE_QUOTA_EXCEEDED"
    (storage.root / "files" / "filled").unlink()
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Mock(free=0))
    with pytest.raises(AppException) as exc:
        storage.check_capacity(100)
    assert exc.value.code == "INSUFFICIENT_DISK_SPACE"
    for key in ("../secret", "/etc/passwd", "files/../../secret"):
        with pytest.raises(AppException):
            storage.resolve_file(key)


async def test_cleanup_expired_records_files_and_terminal_tmp(repository, storage, video):
    task = await make_task(repository, video)
    media = MediaRepository(repository.sessions)
    await execute_download(task.id, TaskService(repository, MemoryBus()), media, storage, FixtureAdapter())
    storage.create_task_directory(task.id)
    active = await make_task(repository, video)
    repository.start(active.id)
    storage.create_task_directory(active.id)
    with repository.sessions.begin() as session:
        session.execute(update(MediaFile).values(expires_at=utcnow() - timedelta(seconds=1)))
    counts = await cleanup_expired_files(storage, media, repository)
    assert counts["files"] == 1 and counts["temporary_directories"] == 1
    assert storage.task_directory(active.id).exists()
    assert not list((storage.root / "files").iterdir())
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MediaFile)) == 0


def test_safe_presets_and_ffmpeg_local_only(video, tmp_path):
    streams = select_streams(video, "video_720_mp4")
    command = ffmpeg_command([tmp_path / "source.mp4"], streams, tmp_path / "output.mp4", "ffmpeg")
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "copy"
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe,crypto"
    with pytest.raises(AppException):
        select_streams(video, "video_1080_mp4")
    with pytest.raises(AppException):
        select_streams(video, "video_720_mp4; rm -rf /")


def test_hls_cannot_fall_back_to_network_ffmpeg():
    from yt_dlp.downloader import hls
    original = hls.FFmpegFD
    with native_network_only():
        assert not hls.FFmpegFD.available()
        with pytest.raises(AppException) as exc:
            hls.FFmpegFD.real_download(None, "output", {"url": "http://127.0.0.1"})
        assert exc.value.code == "UNSUPPORTED_URL"
    assert hls.FFmpegFD is original


@pytest.mark.parametrize("preset_id", ["video_480_mp4", "audio_mp3"])
def test_real_ytdlp_transfer_and_ffmpeg_remux(tmp_path, monkeypatch, preset_id):
    """Actual HTTP transfer and FFmpeg; only extractor/DNS fixture is substituted."""
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from app.modules.download import child

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg integration test requires ffmpeg and ffprobe")
    fixture = tmp_path / "fixture.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x480:r=10",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.5", "-c:v", "libx264", "-threads", "1",
                    "-c:a", "aac", "-shortest", str(fixture)], check=True, timeout=15)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    class FixtureGuard:
        violation = None

        def __init__(self, proxy=None):
            pass

        def installed(self):
            return nullcontext()

        def check_url(self, url):
            assert url.startswith(base + "/")

    info = {"id": "fixture", "extractor_key": "Youtube", "title": "fixture", "duration": 0.5,
            "formats": [{"format_id": "480", "url": base + "/fixture.mp4", "protocol": "http", "ext": "mp4",
                         "height": 480, "width": 640, "vcodec": "avc1.64001e", "acodec": "mp4a.40.2"}]}
    original = child.guarded_downloader

    def factory(guard, options, *, metadata_only=True):
        if metadata_only:
            parser = Mock(extract_info=Mock(return_value=info))
            return nullcontext(parser)
        return original(guard, options, metadata_only=False)

    monkeypatch.setattr(child, "NetworkGuard", FixtureGuard)
    monkeypatch.setattr(child, "guarded_downloader", factory)
    directory = tmp_path / "download"
    directory.mkdir()
    values = []
    try:
        child.download({"url": base + "/watch", "preset_id": preset_id, "platform": "youtube",
                        "platform_video_id": "fixture"}, directory, values.append)
        output = directory / ("output.mp3" if preset_id == "audio_mp3" else "output.mp4")
        assert output.is_file() and output.stat().st_size > 0
        probe = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output)],
                               capture_output=True, text=True, check=True, timeout=5)
        assert {item["codec_name"] for item in json.loads(probe.stdout)["streams"]} == ({"mp3"} if preset_id == "audio_mp3" else {"h264", "aac"})
        assert values[-1]["progress"] == 99
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_cleanup_reclaims_expired_abandoned_task_but_preserves_live_group(repository, storage, video, monkeypatch):
    task = await make_task(repository, video)
    repository.start(task.id)
    directory = storage.create_task_directory(task.id)
    with repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(expires_at=utcnow() - timedelta(seconds=1)))
    counts = await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository)
    assert counts["temporary_directories"] == 1 and not directory.exists()
    assert repository.get(task.id).status == "expired"

    live = await make_task(repository, video)
    repository.start(live.id)
    directory = storage.create_task_directory(live.id)
    (directory / ".process.json").write_text(json.dumps({"pgid": 12345}))
    with repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == live.id).values(expires_at=utcnow() - timedelta(seconds=1)))
    monkeypatch.setattr(os, "killpg", lambda *args: None)
    counts = await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository)
    assert counts["temporary_directories"] == 0 and directory.exists()
    assert repository.get(live.id).status == "running"


async def test_watchdog_size_abort_persists_exact_error_code(repository, storage, video):
    task = await make_task(repository, video)

    class OversizeAdapter:
        async def download(self, payload, directory, **controls):
            code = f"from pathlib import Path; import time; Path({str(directory / 'large')!r}).write_bytes(b'x'*10000); time.sleep(60)"
            await ProcessRunner(5, 1024, interval=0.05, term_grace=0.2).run(
                [sys.executable, "-c", code], directory, cwd=directory, **controls)

    result = await execute_download(task.id, TaskService(repository, MemoryBus()), MediaRepository(repository.sessions), storage, OversizeAdapter())
    assert result["status"] == "failed" and repository.get(task.id).error_code == "FILE_TOO_LARGE"
    assert not storage.task_directory(task.id).exists()


def test_watchdog_permission_probe_does_not_assume_process_group_exited(monkeypatch):
    def denied(*args):
        raise PermissionError
    monkeypatch.setattr(os, "killpg", denied)
    assert ProcessRunner.group_exists(123) is True
