import asyncio
from contextlib import nullcontext
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from test_tasks import MemoryBus, repository
from test_download import storage, video
from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.main import create_app
from app.modules.download.worker import execute_download
from app.modules.media import audio as audio_module
from app.modules.media.audio import AudioAcquirer, select_audio_stream
from app.modules.media.repository import MediaRecord, MediaRepository
from app.modules.media.router import AudioExtractionService, get_audio_service
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import TaskService
from app.modules.transcript.data import make_transcript
from app.modules.transcript.models import Transcript
from app.modules.transcript.providers import FallbackLocalASRProvider, OpenAICompatibleASRProvider, PublicNetworkBackend
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.router import get_transcript_service
from app.modules.transcript.service import TranscriptService
from app.modules.transcript.subtitle_child import select_language
from app.modules.transcript.subtitle_parser import parse_subtitle
from app.modules.transcript.worker import cooperative_asr, execute_transcription
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.repository import VideoRepository


def payload(repository, video):
    video_id = VideoRepository(repository.sessions).get_or_create(video, "https://example.com/watch")
    return {"video_id": str(video_id), "url": "https://example.com/watch", "platform": video.platform,
            "platform_video_id": video.platform_video_id, "title": video.title, "preset_id": "audio_mp3",
            "language": "zh", "content_version": "v1"}


def task_for(repository, video, kind="transcription", **overrides):
    body = {**payload(repository, video), **overrides}
    return repository.create(kind, UUID(body["video_id"]), body)


def seed_media(repository, storage, video, extension="mp3", content=b"existing audio"):
    task = task_for(repository, video, "download")
    repository.start(task.id)
    file_id = uuid4()
    key = f"files/{file_id}.{extension}"
    storage.resolve_file(key).write_bytes(content)
    item = MediaRecord(file_id, task.id, task.video_id, "audio_mp3" if extension == "mp3" else "video_720_mp4",
                       key, f"fixture.{extension}", "audio/mpeg" if extension == "mp3" else "video/mp4",
                       len(content), utcnow() + timedelta(hours=1))
    media = MediaRepository(repository.sessions)
    repository.complete_with(task.id, {}, lambda session: media.register(session, item))
    return item


async def test_audio_reuses_existing_mp3_without_process_or_new_file(repository, storage, video):
    item = seed_media(repository, storage, video)
    task = task_for(repository, video, "audio_extraction")
    runner = Mock(run=AsyncMock(side_effect=AssertionError("must reuse MP3")))
    audio = AudioAcquirer(MediaRepository(repository.sessions), storage, runner)
    result = await execute_download(task.id, TaskService(repository, MemoryBus()), MediaRepository(repository.sessions),
                                    storage, audio, reuse_result=audio.reuse_result)
    assert result["status"] == "completed"
    assert repository.get(task.id).result_payload["file_id"] == str(item.id)
    assert repository.get(task.id).result_payload["reused"] is True
    runner.run.assert_not_awaited()
    assert len(list((storage.root / "files").iterdir())) == 1


async def test_local_video_extraction_uses_ffmpeg_without_network(repository, storage, video, monkeypatch):
    item = seed_media(repository, storage, video, "mp4", b"local video")
    task = task_for(repository, video, "audio_extraction")
    directory = storage.create_task_directory(task.id)

    async def execute(command, directory, **controls):
        assert command[command.index("-i") + 1] == str(storage.resolve_file(item.storage_key))
        assert "-vn" in command and "libmp3lame" in command
        (directory / "output.mp3").write_bytes(b"audio")

    runner = Mock(run=AsyncMock(side_effect=execute))
    monkeypatch.setattr(audio_module.shutil, "which", lambda binary: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio_module, "YtDlpDownloadAdapter", Mock(side_effect=AssertionError("no network")))
    audio = AudioAcquirer(MediaRepository(repository.sessions), storage, runner)
    path = await audio.download(task.input_payload, directory, is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
    assert path.read_bytes() == b"audio"
    runner.run.assert_awaited_once()


async def test_network_audio_is_explicit_audio_only(repository, storage, video, monkeypatch):
    task = task_for(repository, video, "audio_extraction")
    directory = storage.create_task_directory(task.id)

    async def download(body, target, **controls):
        assert body["_audio_only"] is True and body["preset_id"] == "audio_mp3"
        assert "is_cancel_requested" in controls
        (target / "output.mp3").write_bytes(b"audio")
        return target / "output.mp3"

    adapter = Mock(download=AsyncMock(side_effect=download))
    monkeypatch.setattr(audio_module, "YtDlpDownloadAdapter", lambda runner: adapter)
    result = await AudioAcquirer(MediaRepository(repository.sessions), storage, Mock()).download(
        task.input_payload, directory, is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
    assert result.suffix == ".mp3"


def test_audio_selection_never_downloads_muxed_hd(video):
    with pytest.raises(AppException) as error:
        select_audio_stream(video)
    assert error.value.code == "AUDIO_STREAM_UNAVAILABLE"
    audio = [{"url": "https://example.com/a", "vcodec": "none", "acodec": "opus", "abr": 160},
             {"url": "https://example.com/b", "vcodec": "none", "acodec": "aac", "abr": 128}]
    data = ParsedVideo(video.platform, video.platform_video_id, video.title, formats=video.formats + audio)
    assert select_audio_stream(data)["acodec"] == "opus"


@pytest.mark.parametrize("extension,content", [
    ("vtt", "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<b>你好</b>\n\n00:00:01.000 --> 00:00:03.000\n你好\n\n00:00:04.000 --> 00:00:05.000\n世界\n"),
    ("srt", "1\n00:00:00,000 --> 00:00:02,000\n你好\n\n2\n00:00:01,000 --> 00:00:03,000\n你好\n\n3\n00:00:04,000 --> 00:00:05,000\n世界\n"),
    ("json3", json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "你好"}]},
                                       {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "你好"}]},
                                       {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "世界"}]}]})),
])
def test_caption_parsers_dedup_timestamps_and_sha256(extension, content):
    data = parse_subtitle(content, extension, "zh")
    assert data.content_text == "你好\n世界"
    assert data.content_hash == hashlib.sha256("你好\n世界".encode()).hexdigest()
    assert data.segments == [{"start": 0.0, "end": 3.0, "text": "你好"}, {"start": 4.0, "end": 5.0, "text": "世界"}]


def test_manual_subtitles_before_auto_and_region_match():
    info = {"subtitles": {"zh-Hans": [{}]}, "automatic_captions": {"zh": [{}], "en": [{}]}}
    assert select_language(info, "zh") == "zh-Hans"
    assert select_language(info, "en") == "en"
    assert select_language(info, "fr") is None


async def run_pipeline(repository, storage, task, subtitles, audio, provider):
    return await execute_transcription(task.id, TaskService(repository, MemoryBus()), TranscriptRepository(repository.sessions),
                                        MediaRepository(repository.sessions), storage, subtitles, audio, provider)


async def test_subtitle_first_never_calls_audio_or_asr(repository, storage, video):
    task = task_for(repository, video)
    data = parse_subtitle("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n", "vtt", "zh")
    subtitles = Mock(fetch=AsyncMock(return_value=data))
    audio = Mock(acquire=AsyncMock(side_effect=AssertionError("must not extract")))
    asr = Mock(model="cloud-a", transcribe=AsyncMock(side_effect=AssertionError("must not transcribe")))
    result = await run_pipeline(repository, storage, task, subtitles, audio, asr)
    assert result["status"] == "completed"
    record = TranscriptRepository(repository.sessions).get(UUID(repository.get(task.id).result_payload["transcript_id"]))
    assert record.source_type == "subtitle" and record.asr_model == "none"
    assert record.content_hash == data.content_hash
    assert not storage.task_directory(task.id).exists()
    audio.acquire.assert_not_awaited()
    asr.transcribe.assert_not_awaited()


async def test_no_subtitle_uses_asr_and_cleans_ephemeral_audio(repository, storage, video):
    task = task_for(repository, video)

    async def acquire(payload, directory, **controls):
        path = directory / "audio.mp3"
        path.write_bytes(b"temporary audio")
        return path

    data = make_transcript("zh", [{"start": 0, "end": 1.25, "text": "云端识别结果"}])
    provider = Mock(model="cloud-a", transcribe=AsyncMock(return_value=data))
    result = await run_pipeline(repository, storage, task, Mock(fetch=AsyncMock(return_value=None)),
                                 Mock(acquire=AsyncMock(side_effect=acquire)), provider)
    assert result["status"] == "completed"
    provider.transcribe.assert_awaited_once()
    item = TranscriptRepository(repository.sessions).get(UUID(repository.get(task.id).result_payload["transcript_id"]))
    assert item.source_type == "asr" and item.asr_model == "cloud-a"
    assert item.content_hash == hashlib.sha256("云端识别结果".encode()).hexdigest()
    assert not storage.task_directory(task.id).exists()


async def test_cache_hit_avoids_all_io_and_model_language_version_are_isolated(repository, storage, video):
    original = task_for(repository, video)
    transcripts = TranscriptRepository(repository.sessions)
    data = make_transcript("zh", [{"start": 0, "end": 1, "text": "缓存文本"}])
    with repository.sessions.begin() as session:
        cached = transcripts.store(session, original.video_id, original.id, "asr", data, "v1", "cloud-a")
    assert transcripts.find_cache(original.video_id, "en", "v1", "cloud-a") is None
    assert transcripts.find_cache(original.video_id, "zh", "v2", "cloud-a") is None
    assert transcripts.find_cache(original.video_id, "zh", "v1", "cloud-b") is None
    task = task_for(repository, video)
    subtitles, audio = Mock(fetch=AsyncMock()), Mock(acquire=AsyncMock())
    provider = Mock(model="cloud-a", transcribe=AsyncMock())
    started = time.monotonic()
    result = await run_pipeline(repository, storage, task, subtitles, audio, provider)
    assert time.monotonic() - started < 1 and result["status"] == "completed"
    assert repository.get(task.id).result_payload == {"transcript_id": str(cached.id), "source_type": "asr",
                                                      "content_hash": cached.content_hash, "cache_hit": True}
    subtitles.fetch.assert_not_awaited()
    audio.acquire.assert_not_awaited()
    provider.transcribe.assert_not_awaited()


async def test_missing_key_fails_clearly_without_downloading_audio(repository, storage, video):
    task = task_for(repository, video)
    audio = Mock(acquire=AsyncMock())
    result = await run_pipeline(repository, storage, task, Mock(fetch=AsyncMock(return_value=None)), audio, FallbackLocalASRProvider())
    assert result["status"] == "failed" and repository.get(task.id).error_code == "ASR_NOT_CONFIGURED"
    audio.acquire.assert_not_awaited()
    assert not storage.task_directory(task.id).exists()


async def test_asr_can_be_cooperatively_cancelled():
    provider = Mock(transcribe=AsyncMock(side_effect=lambda *args: None))
    exited = asyncio.Event()

    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            exited.set()

    provider.transcribe.side_effect = slow
    cancelled = AsyncMock(side_effect=[False, True])
    with pytest.raises(AppException) as error:
        await cooperative_asr(provider, Path("audio.mp3"), "zh", cancelled)
    assert error.value.code == "TASK_CANCELLED" and exited.is_set()


async def test_cloud_asr_multipart_and_verbose_segments(tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture audio")
    requests = []

    async def handler(request):
        requests.append(request)
        body = await request.aread()
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.url.path == "/v1/audio/transcriptions"
        for value in (b"verbose_json", b"whisper-1", b"timestamp_granularities[]", b"fixture audio", b"segment"):
            assert value in body
        return httpx.Response(200, json={"language": "chinese", "segments": [{"start": 0, "end": 1, "text": "你好"}]})

    provider = OpenAICompatibleASRProvider("https://asr.example/v1", "test-secret", "whisper-1", transport=httpx.MockTransport(handler))
    data = await provider.transcribe(path, "zh")
    assert data.language == "zh" and data.content_text == "你好" and len(requests) == 1


@pytest.mark.parametrize("kind,code", [("timeout", "ASR_TIMEOUT"), ("unauthorized", "ASR_API_ERROR"), ("bad_json", "ASR_INVALID_RESPONSE")])
async def test_cloud_asr_failure_codes_do_not_leak_secrets(tmp_path, kind, code, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"audio")
    calls = []

    async def handler(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("test-secret sensitive endpoint")
        if kind == "unauthorized":
            return httpx.Response(401, text="test-secret")
        return httpx.Response(200, json={"text": "no timestamps"})

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    provider = OpenAICompatibleASRProvider("https://asr.example/v1", "test-secret", "whisper-1", transport=httpx.MockTransport(handler))
    with pytest.raises(AppException) as error:
        await provider.transcribe(path, "zh")
    assert error.value.code == code and "test-secret" not in error.value.message
    assert len(calls) == (2 if kind == "timeout" else 1)


async def test_cloud_asr_retry_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"audio")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503, text="secret")

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with pytest.raises(AppException):
        await OpenAICompatibleASRProvider("https://asr.example/v1", "key", "whisper-1", transport=httpx.MockTransport(handler)).transcribe(path, None)
    assert count == 2


async def test_asr_network_pins_public_dns_and_rejects_mixed_results(monkeypatch):
    backend = Mock(connect_tcp=AsyncMock(return_value=object()))
    wrapper = PublicNetworkBackend(backend)
    resolve = AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    await wrapper.connect_tcp("asr.example", 443, timeout=1)
    assert backend.connect_tcp.call_args.args[0] == "93.184.216.34"
    resolve.return_value.append((2, 1, 6, "", ("127.0.0.1", 443)))
    with pytest.raises(AppException) as error:
        await wrapper.connect_tcp("asr.example", 443, timeout=1)
    assert error.value.code == "SSRF_BLOCKED"


async def test_audio_and_transcription_api_contracts(repository, storage, video):
    body = payload(repository, video)
    resolver = Mock(resolve=AsyncMock(return_value=body))
    tasks = TaskService(repository, MemoryBus(), Mock())
    transcripts = TranscriptRepository(repository.sessions)
    audio_service = AudioExtractionService(resolver, tasks)
    service = TranscriptService(resolver, tasks, transcripts)
    app = create_app()
    app.dependency_overrides[get_audio_service] = lambda: audio_service
    app.dependency_overrides[get_transcript_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/audio/extractions", json={"url": body["url"], "format": "mp3"})
        assert response.status_code == 202 and response.json()["data"]["status"] == "queued"
        response = await client.post("/api/v1/transcriptions", json={"url": body["url"], "language": "zh"})
        assert response.status_code == 202
        task = repository.get(UUID(response.json()["data"]["task_id"]))
        assert task.task_type == "transcription"
        data = make_transcript("zh", [{"start": 0, "end": 1, "text": "你好"}])
        with repository.sessions.begin() as session:
            item = transcripts.store(session, task.video_id, task.id, "subtitle", data, "v1", "none")
        response = await client.get(f"/api/v1/transcriptions/{item.id}")
        assert response.status_code == 200 and response.json()["data"]["segments"] == data.segments
        assert response.json()["data"]["source_type"] == "subtitle"
        missing = await client.get(f"/api/v1/transcriptions/{uuid4()}")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "TRANSCRIPT_NOT_FOUND"


@pytest.mark.parametrize("inline_srt", [False, True])
def test_real_subtitle_download_skips_every_media_stream(tmp_path, monkeypatch, inline_srt):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from app.modules.transcript import subtitle_child

    (tmp_path / "captions.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n字幕优先\n", encoding="utf-8")
    requested = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            return super().do_GET()

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

    info = {"id": "fixture", "extractor_key": "Youtube", "title": "fixture", "duration": 1,
            "subtitles": {"zh": [{"ext": "vtt", "url": base + "/captions.vtt"}]},
            "formats": [{"format_id": "video", "url": base + "/forbidden.mp4", "protocol": "http", "ext": "mp4",
                         "height": 720, "vcodec": "avc1", "acodec": "mp4a"}]}
    if inline_srt:
        info["subtitles"] = {"zh": [{"ext": "srt", "data": "1\n00:00:00,000 --> 00:00:01,000\n字幕优先\n"}]}
    original = subtitle_child.guarded_downloader

    def factory(guard, options, *, metadata_only=True):
        if metadata_only:
            assert options.get("listsubtitles") is True
            return nullcontext(Mock(extract_info=Mock(return_value=info)))
        assert options["listsubtitles"] is False
        assert options["skip_download"] and options["writesubtitles"] and options["writeautomaticsub"]
        return original(guard, options, metadata_only=False)

    monkeypatch.setattr(subtitle_child, "NetworkGuard", FixtureGuard)
    monkeypatch.setattr(subtitle_child, "guarded_downloader", factory)
    directory = tmp_path / "subtitles"
    directory.mkdir()
    values = []
    try:
        subtitle_child.fetch({"url": base + "/watch", "platform": "youtube", "platform_video_id": "fixture", "language": "zh"},
                             directory, values.append)
        assert values[-1]["subtitle_found"] is True
        assert requested == ([] if inline_srt else ["/captions.vtt"])
        extension = "srt" if inline_srt else "vtt"
        assert all(path.suffix == f".{extension}" for path in directory.iterdir())
        data = parse_subtitle((directory / values[-1]["filename"]).read_text(), extension, "zh")
        assert data.content_text == "字幕优先"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_cancel_before_transcript_commit_leaves_no_cache_record(repository, storage, video):
    task = task_for(repository, video)
    data = make_transcript("zh", [{"start": 0, "end": 1, "text": "不能提交"}])

    async def fetch(*args, **kwargs):
        repository.request_cancel(task.id)
        return data

    result = await run_pipeline(repository, storage, task, Mock(fetch=AsyncMock(side_effect=fetch)),
                                 Mock(acquire=AsyncMock()), Mock(model="cloud-a", transcribe=AsyncMock()))
    assert result["status"] == "cancelled"
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Transcript)) == 0
    assert not storage.task_directory(task.id).exists()


async def test_asr_reuses_audio_without_deleting_shared_file(repository, storage, video):
    item = seed_media(repository, storage, video)
    task = task_for(repository, video)
    path = storage.resolve_file(item.storage_key)
    provider = Mock(model="cloud-a", transcribe=AsyncMock(return_value=make_transcript("zh", [{"start": 0, "end": 1, "text": "复用音轨"}])))
    audio = AudioAcquirer(MediaRepository(repository.sessions), storage, Mock(run=AsyncMock()))
    result = await run_pipeline(repository, storage, task, Mock(fetch=AsyncMock(return_value=None)), audio, provider)
    assert result["status"] == "completed" and path.is_file()
    assert provider.transcribe.call_args.args[0] == path
    assert not storage.task_directory(task.id).exists()


def test_bilibili_subtitle_language_and_danmaku_filter():
    from app.modules.transcript.subtitle_child import select_language
    from app.modules.video.parsers.ytdlp import metadata_from_info
    info = {"id": "BVfixture", "extractor_key": "BiliBili", "subtitles": {
        "danmaku": [{"ext": "xml"}], "ai-zh": [{"ext": "srt", "data": "字幕"}]}}
    assert metadata_from_info(info).subtitle_languages == ["ai-zh"]
    assert select_language(info, "zh") == "ai-zh"
    info["subtitles"].pop("ai-zh")
    assert metadata_from_info(info).subtitle_languages == []
    assert select_language(info, None) is None


async def test_qwen_asr_sends_data_uri_and_extracts_text(tmp_path):
    from app.modules.transcript.providers import QwenASRProvider
    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"test audio")

    def handle(request):
        import base64
        body = json.loads(request.content)
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert body["model"] == "qwen3-asr-flash"
        assert body["messages"][0]["content"][0]["input_audio"]["format"] == "mp3"
        assert body["messages"][0]["content"][0]["input_audio"]["data"] == "data:audio/mpeg;base64," + base64.b64encode(b"test audio").decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "真实转写测试"}}]})

    provider = QwenASRProvider("https://example.com/compatible-mode/v1", "test-key", "qwen3-asr-flash", transport=httpx.MockTransport(handle))
    result = await provider._transcribe_chunk(audio, "zh")
    assert result.content_text == "真实转写测试"


@pytest.mark.parametrize("status,body,code", [
    (404, {"error": {"code": "model_not_supported", "message": "private upstream details"}}, "ASR_MODEL_UNSUPPORTED"),
    (404, {}, "ASR_ENDPOINT_NOT_FOUND"),
    (401, {}, "ASR_AUTH_ERROR"),
    (403, {}, "ASR_AUTH_ERROR"),
    (429, {}, "ASR_RATE_LIMITED"),
])
async def test_qwen_asr_actionable_errors(tmp_path, status, body, code):
    from app.modules.transcript.providers import QwenASRProvider
    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"audio")
    provider = QwenASRProvider("https://example.com/v1", "test-key", "qwen3-asr-flash", transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    with pytest.raises(AppException) as exc:
        await provider._transcribe_chunk(audio, None)
    assert exc.value.code == code
    assert "private upstream details" not in exc.value.message


@pytest.mark.parametrize("upload_mb, expected_chunks", [(25, 2), (1, 4)])
async def test_qwen_splits_long_audio_with_real_ranges_and_cleans(tmp_path, upload_mb, expected_chunks):
    import wave
    from app.modules.transcript.providers import QwenASRProvider
    audio = tmp_path / "long.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * (16000 * 125))
    calls = []

    def handle(request):
        import base64
        body = json.loads(request.content)
        uri = body["messages"][0]["content"][0]["input_audio"]["data"]
        assert uri.startswith("data:audio/wav;base64,")
        assert body["messages"][0]["content"][0]["input_audio"]["format"] == "wav"
        chunk = base64.b64decode(uri.split(",", 1)[1])
        assert len(chunk) <= upload_mb * 1024 * 1024
        calls.append(chunk)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"分段 {len(calls)}", "annotations": [{"language": "zh"}]}}]})

    provider = QwenASRProvider("https://example.com/v1", "test", "qwen3-asr-flash", max_upload_mb=upload_mb, transport=httpx.MockTransport(handle))
    data = await provider.transcribe(audio, None)
    assert len(calls) == expected_chunks
    assert len(data.segments) == expected_chunks
    assert data.segments[0]["start"] == 0
    assert data.segments[-1]["end"] == pytest.approx(125, abs=.1)
    for previous, current in zip(data.segments, data.segments[1:]):
        assert current["start"] == pytest.approx(previous["end"])
    assert data.language == "zh"
    assert not list(tmp_path.glob("asr-chunks-*"))
    assert audio.exists()


@pytest.mark.parametrize("failure", ["cancel", "upstream"])
async def test_qwen_chunk_failure_cleans_temporary_files_and_preserves_source(tmp_path, monkeypatch, failure):
    import asyncio
    import wave
    from app.modules.transcript import providers

    source = tmp_path / "original.mp3"
    source.write_bytes(b"original audio")

    async def split(runner, command, directory, **kwargs):
        for index in range(2):
            with wave.open(str(directory / f"chunk-{index:05d}.wav"), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * 16000)
        if failure == "cancel":
            raise asyncio.CancelledError
        return {}

    monkeypatch.setattr(providers.shutil, "which", lambda _: "/opt/ffmpeg")
    monkeypatch.setattr(providers.ProcessRunner, "run", split)
    provider = providers.QwenASRProvider("https://example.com/v1", "key", "qwen3-asr-flash")
    provider._transcribe_chunk = AsyncMock(side_effect=AppException("ASR_API_ERROR", "upstream failed", 502))
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else AppException):
        await provider.transcribe(source, "zh")
    assert provider._transcribe_chunk.await_count == (0 if failure == "cancel" else 1)
    assert source.read_bytes() == b"original audio"
    assert not list(tmp_path.glob("asr-chunks-*"))


async def test_qwen_reports_upstream_audio_format_error(tmp_path):
    from app.modules.transcript.providers import QwenASRProvider
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"test")
    provider = QwenASRProvider("https://example.com/v1", "key", "qwen3-asr-flash", transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {"code": "UNSUPPORTED_FORMAT", "message": "private upstream details"}})))
    with pytest.raises(AppException) as error:
        await provider._transcribe_chunk(audio, None)
    assert error.value.code == "ASR_AUDIO_FORMAT_UNSUPPORTED"
    assert "private upstream details" not in error.value.message
