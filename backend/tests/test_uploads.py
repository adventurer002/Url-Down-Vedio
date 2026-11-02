"""Private upload business flow. Real FFmpeg/ffprobe, isolated DB, no paid model calls."""
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from test_tasks import repository, MemoryBus
from test_download import storage
from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.infrastructure.storage.local import LocalStorageAdapter
from app.main import create_app
from app.modules.ai.models import AIResult
from app.modules.ai.providers import HeuristicLLMProvider
from app.modules.ai.repository import AIRepository
from app.modules.ai.worker import execute_ai
from app.modules.auth.dependencies import required_user
from app.modules.auth.repository import UserRepository
from app.modules.billing.repository import AllowanceRepository
from app.modules.billing.router import get_billing_service
from app.modules.billing.service import BillingService
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.worker import execute_question
from app.modules.media.audio import AudioAcquirer
from app.modules.media.repository import MediaRepository
from app.modules.tasks.models import Task
from app.modules.tasks.service import TaskService
from app.modules.transcript.data import make_transcript
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.service import TranscriptService, TranscriptionPipeline
from app.modules.transcript.worker import execute_transcription
from app.modules.uploads.models import Upload
from app.modules.uploads.probe import FFprobeAdapter
from app.modules.uploads.router import get_upload_service
from app.modules.uploads.service import checked_filename, upload_service
from app.modules.uploads.worker import execute_probe
from app.modules.video.models import Video
from app.modules.video.repository import VideoRepository
from app.modules.video.resolver import VideoResolver


@pytest.fixture(scope="module")
def media_samples(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe required for real media upload tests")
    directory = tmp_path_factory.mktemp("upload-samples")
    prefix = [ffmpeg, "-nostdin", "-v", "error", "-y"]
    subprocess.run(prefix + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
        str(directory / "sample.mp3")], timeout=10, check=True)
    subprocess.run(prefix + ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest",
        str(directory / "sample.mp4")], timeout=10, check=True)
    subprocess.run(prefix + ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-c:v", "libx264",
        str(directory / "silent.mp4")], timeout=10, check=True)
    for filename, codec in (("sample.m4a", "aac"), ("lossless.m4a", "alac")):
        subprocess.run(prefix + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", codec,
            str(directory / filename)], timeout=10, check=True)
    return {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.fixture
def uploads(repository, storage, monkeypatch):
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", True)
    monkeypatch.setattr(settings, "ASR_PROVIDER", "mock")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "heuristic")
    monkeypatch.setattr(settings, "ASR_MOCK_TEXT", "缓存键包含模型和提示词版本，数据库事务保证额度只结算一次。")
    monkeypatch.setattr(settings, "STORAGE_LOCAL_PATH", storage.root)
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 32)
    users = UserRepository(repository.sessions)
    user = users.subscription("upload-owner", True, login=True)
    other = users.subscription("upload-other", True, login=True)
    tasks = TaskService(repository, MemoryBus(), Mock())
    service = upload_service(repository.sessions, storage, tasks)
    videos = VideoRepository(repository.sessions)
    resolver = VideoResolver(Mock(parse=AsyncMock(side_effect=AssertionError("no URL parser for uploads"))), videos, service)
    billing = BillingService(AllowanceRepository(repository.sessions), resolver, videos, tasks)
    return SimpleNamespace(service=service, tasks=tasks, repository=repository, storage=storage, users=users,
        user=user, other=other, billing=billing, videos=videos, resolver=resolver,
        transcripts=TranscriptRepository(repository.sessions), results=AIRepository(repository.sessions))


async def chunks(content):
    for start in range(0, len(content), 137):
        yield content[start:start + 137]


async def received(u, content, filename="sample.mp3", user=None):
    owner = user or u.user
    item = await u.service.create(owner.id, filename, len(content))
    return await u.service.receive(item["id"], owner.id, chunks(content))


async def ready(u, content, filename="sample.mp3", user=None):
    owner = user or u.user
    item = await received(u, content, filename, owner)
    created = await u.service.complete(item["id"], owner.id)
    outcome = await execute_probe(created["task_id"], u.tasks, u.service, FFprobeAdapter(ProcessRunner(10, 1024**2)))
    assert outcome["status"] == "completed", u.repository.get(created["task_id"])
    return await u.service.get(item["id"], owner.id)


async def quoted(u, item, action="transcription", user=None):
    owner = user or u.user
    quote = await u.billing.quote(owner.id, None, action, "audio_mp3", "zh", upload_id=item["id"])
    return await u.billing.confirm(owner.id, UUID(quote["quote_id"]))


async def transcribe(u, item, provider=None):
    task = await quoted(u, item)
    audio = AudioAcquirer(MediaRepository(u.repository.sessions), u.storage, ProcessRunner(10, 32 * 1024**2))
    subtitles = Mock(fetch=AsyncMock(side_effect=AssertionError("uploaded files never fetch platform captions")))
    from app.modules.transcript.providers import get_asr_provider
    result = await execute_transcription(task.id, u.tasks, u.transcripts, MediaRepository(u.repository.sessions),
        u.storage, subtitles, audio, provider or get_asr_provider())
    return task, result


@pytest.mark.parametrize("filename", ["sample.mp3", "sample.mp4", "sample.m4a", "lossless.m4a"])
async def test_real_probe_transcription_and_no_duplicate_asr_charge(uploads, media_samples, filename):
    u = uploads
    before = u.billing.repository.balance(u.user.id)["balances"]
    item = await ready(u, media_samples[filename], filename)
    assert item["media_type"] == ("audio" if filename.endswith(("mp3", "m4a")) else "video")
    assert item["content_sha256"] == hashlib.sha256(media_samples[filename]).hexdigest()
    assert item["duration_seconds"] in {1, 2}
    assert u.billing.repository.balance(u.user.id)["balances"] == before
    task, result = await transcribe(u, item)
    assert result["status"] == "completed", u.repository.get(task.id)
    transcript = await TranscriptService(None, u.tasks, u.transcripts).get(
        UUID(u.repository.get(task.id).result_payload["transcript_id"]), u.user)
    assert transcript["source_type"] == "asr" and transcript["content_text"]
    after = u.billing.repository.balance(u.user.id)["balances"]
    assert after["asr_seconds"]["available"] < before["asr_seconds"]["available"]
    task, result = await transcribe(u, item, Mock(model="mock-asr-v1", transcribe=AsyncMock(side_effect=AssertionError("cache"))))
    assert result["status"] == "completed"
    assert u.repository.get(task.id).result_payload["cache_hit"]
    assert u.billing.repository.balance(u.user.id)["balances"] == after
    assert not list((u.storage.root / "tmp").glob("*/asr-*"))


@pytest.mark.parametrize("filename", ["sample.mp3", "sample.m4a"])
async def test_full_ai_mindmap_question_library_survive_source_deletion(uploads, media_samples, filename):
    u = uploads
    item = await ready(u, media_samples[filename], filename)
    task = await quoted(u, item, "ai_mindmap")
    outcome = await execute_ai(task.id, u.tasks, u.results,
        TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), HeuristicLLMProvider())
    assert outcome["status"] == "completed", u.repository.get(task.id)
    value = u.repository.get(task.id).result_payload
    assert value["summary_result_id"] and value["result_id"] and value["transcript_id"]
    transcript_id = UUID(value["transcript_id"])
    knowledge = KnowledgeRepository(u.repository.sessions)
    service = KnowledgeService(knowledge, u.transcripts, u.results, u.videos, u.tasks, u.billing.repository)
    saved = await service.save(u.user, transcript_id, UUID(value["summary_result_id"]), ["录音"])
    assert saved["video"]["source_kind"] == "upload" and saved["video"]["source_url"] is None
    before = u.billing.repository.balance(u.user.id)["balances"]
    await u.service.delete(item["id"], u.user.id)
    assert not u.service.files.path(item["id"]).exists()
    quote = await service.quote(u.user, transcript_id, "缓存键包含哪些版本？", "zh")
    assert quote["maximum"]["asr_seconds"] == 0
    question = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert (await execute_question(question.id, u.tasks, knowledge, u.transcripts, HeuristicLLMProvider()))["status"] == "completed"
    answer = knowledge.answer(u.user.id, UUID(u.repository.get(question.id).result_payload["answer_id"]))
    assert answer["citations"] and answer["citations"][0]["approximate"]
    assert knowledge.saved(u.user.id, saved["id"])["content_text"]
    after = u.billing.repository.balance(u.user.id)["balances"]
    assert after["asr_seconds"] == before["asr_seconds"]
    assert after["ai_generations"]["available"] == before["ai_generations"]["available"] - 1


async def test_same_content_is_deduped_only_within_owner_and_ai_scope_is_private(uploads, media_samples):
    u = uploads
    first = await ready(u, media_samples["sample.mp3"])
    repeated = await ready(u, media_samples["sample.mp3"])
    other = await ready(u, media_samples["sample.mp3"], user=u.other)
    different_recording = await ready(u, media_samples["sample.mp4"], "sample.mp4")
    assert first["video_id"] == repeated["video_id"] != other["video_id"]
    ids, scopes = [], []
    for item, owner in ((first, u.user), (other, u.other), (different_recording, u.user)):
        task = await quoted(u, item, "ai_summary", owner)
        outcome = await execute_ai(task.id, u.tasks, u.results,
            TranscriptionPipeline(u.tasks, u.repository.sessions, u.storage), HeuristicLLMProvider())
        assert outcome["status"] == "completed", u.repository.get(task.id)
        result = u.results.get(UUID(u.repository.get(task.id).result_payload["result_id"]))
        ids.append(result.id)
        scopes.append(u.results.scope_for(u.transcripts.get(result.transcript_id)))
        assert u.results.find_cache(result.source_content_hash, "summary", result.llm_model, result.prompt_version, "zh") is None
    assert len(set(ids)) == len(set(scopes)) == 3
    assert u.results.get(ids[2]).video_id == different_recording["video_id"]
    with pytest.raises(AppException):
        await u.service.resolve(first["id"], u.other.id)
    with pytest.raises(AppException):
        await u.service.delete(first["id"], u.other.id)
    from app.modules.ai.service import AIService
    with pytest.raises(AppException):
        await AIService(None, u.tasks, u.results).get(ids[0], u.other)


@pytest.mark.parametrize("content,filename,code", [
    (b"not a media file", "bad.mp3", "UPLOAD_INVALID_MEDIA"),
    ("sample.mp3", "disguised.mp4", "UPLOAD_FORMAT_MISMATCH"),
    ("silent.mp4", "silent.mp4", "UPLOAD_NO_AUDIO"),
    ("sample.mp3", "disguised.m4a", "UPLOAD_FORMAT_MISMATCH"),
    ("sample.mp4", "video.m4a", "UPLOAD_FORMAT_MISMATCH"),
    (b"not a media file", "bad.m4a", "UPLOAD_INVALID_MEDIA"),
    (b"#EXTM3U\nhttp://169.254.169.254/latest/meta-data", "attack.mp4", "UPLOAD_INVALID_MEDIA"),
])
async def test_invalid_no_audio_and_network_references_are_rejected(uploads, media_samples, content, filename, code):
    u = uploads
    content = media_samples[content] if isinstance(content, str) else content
    item = await received(u, content, filename)
    created = await u.service.complete(item["id"], u.user.id)
    await execute_probe(created["task_id"], u.tasks, u.service, FFprobeAdapter(ProcessRunner(10, 1024**2)))
    task = u.repository.get(created["task_id"])
    assert task.status == "failed" and task.error_code == code
    assert not u.service.files.path(item["id"]).exists()
    assert (await u.service.get(item["id"], u.user.id))["status"] == "failed"
    with u.repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Video)) == 0


async def test_complete_is_idempotent_and_cancel_does_not_register_source(uploads, media_samples):
    u = uploads
    item = await received(u, media_samples["sample.mp3"])
    first = await u.service.complete(item["id"], u.user.id)
    with u.service.files.lock(item["id"], shared=True):
        second = await u.service.complete(item["id"], u.user.id)
    assert first["task_id"] == second["task_id"]
    u.repository.request_cancel(first["task_id"])
    probe = Mock(inspect=AsyncMock(side_effect=AssertionError("cancelled tasks do not probe")))
    assert (await execute_probe(first["task_id"], u.tasks, u.service, probe))["status"] == "cancelled"
    assert (await u.service.get(item["id"], u.user.id))["status"] == "failed"
    await u.service.delete(item["id"], u.user.id)
    with u.repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Video)) == 0


async def test_running_cancel_and_failure_release_all_allowances(uploads, media_samples):
    u = uploads
    item = await ready(u, media_samples["sample.mp3"])
    for mode in ("cancel", "failure"):
        before = u.billing.repository.balance(u.user.id)["balances"]
        task = await quoted(u, item)
        async def run(path, language):
            with pytest.raises(AppException) as error:
                await u.service.delete(item["id"], u.user.id)
            assert error.value.code == "UPLOAD_IN_USE"
            if mode == "cancel":
                u.repository.request_cancel(task.id)
                await asyncio.sleep(.6)
            raise AppException("ASR_TIMEOUT", "识别超时", 504)
        from app.modules.transcript.providers import get_asr_provider
        provider = SimpleNamespace(model=get_asr_provider().model, transcribe=run)
        audio = AudioAcquirer(MediaRepository(u.repository.sessions), u.storage, ProcessRunner(10, 32 * 1024**2))
        result = await execute_transcription(task.id, u.tasks, u.transcripts, MediaRepository(u.repository.sessions),
            u.storage, Mock(fetch=AsyncMock(side_effect=AssertionError())), audio, provider)
        assert result["status"] == ("cancelled" if mode == "cancel" else "failed")
        assert u.billing.repository.balance(u.user.id)["balances"] == before
        assert u.service.files.path(item["id"]).is_file()


async def test_receive_limits_retry_timeouts_and_expiry(uploads, monkeypatch):
    u = uploads
    item = await u.service.create(u.user.id, "test.mp3", 4)
    for body, code in ((b"12345", "FILE_TOO_LARGE"), (b"12", "UPLOAD_SIZE_MISMATCH")):
        with pytest.raises(AppException) as error:
            await u.service.receive(item["id"], u.user.id, chunks(body))
        assert error.value.code == code
        assert not u.service.files.path(item["id"]).exists()
    monkeypatch.setattr(settings, "UPLOAD_IDLE_TIMEOUT_SECONDS", .02)
    async def stalled():
        yield b"1"
        await asyncio.sleep(10)
    with pytest.raises(AppException) as error:
        await u.service.receive(item["id"], u.user.id, stalled())
    assert error.value.code == "UPLOAD_TIMEOUT"
    assert not list(u.service.files.directory(item["id"]).glob("*.part"))
    await u.service.receive(item["id"], u.user.id, chunks(b"1234"))
    assert u.service.files.path(item["id"]).read_bytes() == b"1234"
    with u.repository.sessions.begin() as session:
        session.execute(update(Upload).where(Upload.id == item["id"]).values(expires_at=settings_date()))
    with pytest.raises(AppException) as error:
        await u.service.complete(item["id"], u.user.id)
    assert error.value.code == "UPLOAD_EXPIRED"


def settings_date():
    from app.modules.billing.policy import now
    return now() - timedelta(seconds=1)


async def test_cleanup_respects_queued_tasks_and_preserves_results(uploads, media_samples):
    u = uploads
    item = await ready(u, media_samples["sample.mp3"])
    task, result = await transcribe(u, item)
    transcript_id = UUID(u.repository.get(task.id).result_payload["transcript_id"])
    pending = await quoted(u, item)
    with u.repository.sessions.begin() as session:
        session.execute(update(Upload).where(Upload.id == item["id"]).values(expires_at=settings_date()))
    counts = await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository)
    assert counts["uploads"] == 0
    assert u.service.files.path(item["id"]).is_file()
    u.repository.request_cancel(pending.id)
    counts = await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository)
    assert counts["uploads"] == 1
    assert not u.service.files.path(item["id"]).exists()
    assert (await TranscriptService(None, u.tasks, u.transcripts).get(transcript_id, u.user))["content_text"]
    assert (await cleanup_expired_files(u.storage, MediaRepository(u.repository.sessions), u.repository))["uploads"] == 0


async def test_storage_reserves_capacity_and_short_leases_allow_parallel_admission(uploads):
    u = uploads
    small = LocalStorageAdapter(u.storage.root / "small", quota_mb=1, min_free_mb=0)
    first, second = uuid4(), uuid4()
    small.reserve(first, 700000, f"uploads/{first}")
    with pytest.raises(AppException) as error:
        small.reserve(second, 700000, f"uploads/{second}")
    assert error.value.code == "STORAGE_QUOTA_EXCEEDED"
    small.release_reservation(first)
    small.reserve(second, 700000, f"uploads/{second}")
    small.release_reservation(second)
    with u.storage.reservation(uuid4(), 1000):
        with u.storage.lease():
            pass  # Long processing no longer holds the admission/cleanup mutex.


async def test_api_auth_private_source_xor_and_quote_confirmation(uploads, media_samples):
    u = uploads
    app = create_app()
    app.dependency_overrides[get_upload_service] = lambda: u.service
    app.dependency_overrides[get_billing_service] = lambda: u.billing
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/uploads", json={"filename": "sample.mp3", "size_bytes": 5})
        assert response.status_code == 401
        app.dependency_overrides[required_user] = lambda: u.user
        content = media_samples["sample.mp3"]
        response = await client.post("/api/v1/uploads", json={"filename": "sample.mp3", "size_bytes": len(content)})
        assert response.status_code == 201
        identifier = response.json()["data"]["id"]
        assert "user_id" not in response.json()["data"]
        response = await client.put(f"/api/v1/uploads/{identifier}/content", content=content)
        assert response.status_code == 200
        response = await client.post(f"/api/v1/uploads/{identifier}/complete")
        assert response.status_code == 202
        task_id = UUID(response.json()["data"]["task_id"])
        await execute_probe(task_id, u.tasks, u.service, FFprobeAdapter(ProcessRunner(10, 1024**2)))
        for body in ({"action": "transcription"}, {"action": "transcription", "upload_id": identifier, "url": "https://example.com"}):
            assert (await client.post("/api/v1/billing/quotes", json=body)).status_code == 422
        response = await client.post("/api/v1/billing/quotes", json={"upload_id": identifier, "action": "transcription"})
        assert response.status_code == 200, response.text
        quote_id = response.json()["data"]["quote_id"]
        response = await client.post("/api/v1/billing/tasks", json={"quote_id": quote_id})
        assert response.status_code == 202
        app.dependency_overrides[required_user] = lambda: u.other
        for method, path in (("GET", ""), ("DELETE", ""), ("POST", "/complete"), ("PUT", "/content")):
            response = await client.request(method, f"/api/v1/uploads/{identifier}{path}", content=content if method == "PUT" else None)
            assert response.status_code == 404
        response = await client.post("/api/v1/billing/quotes", json={"upload_id": identifier, "action": "ai_summary"})
        assert response.status_code == 404


@pytest.mark.parametrize("filename", ["bad.txt", "bad.m3u8", "bad.wav", "no-extension", "../.mp3"])
def test_filename_allowlist(filename):
    with pytest.raises(AppException):
        checked_filename(filename)


async def test_qwen_chunks_use_task_directory_and_preserve_private_original(uploads, media_samples, monkeypatch):
    u = uploads
    from app.modules.transcript.providers import QwenASRProvider
    monkeypatch.setattr(settings, "ASR_PROVIDER", "qwen")
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "ASR_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "ASR_MODEL", "qwen3-asr-flash")
    item = await ready(u, media_samples["sample.mp3"])
    observed = []
    def respond(request):
        assert list((u.storage.root / "tmp").glob("*/asr-chunks-*"))
        assert not list(u.service.files.directory(item["id"]).glob("asr-chunks-*"))
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "缓存键包含模型版本。"}}]})
    provider = QwenASRProvider("https://example.com/v1", "test-key", "qwen3-asr-flash",
        transport=httpx.MockTransport(respond))
    task, outcome = await transcribe(u, item, provider)
    assert outcome["status"] == "completed", u.repository.get(task.id)
    assert observed
    assert u.service.files.path(item["id"]).read_bytes() == media_samples["sample.mp3"]
    assert not list((u.storage.root / "tmp").glob("*/asr-chunks-*"))


async def test_resource_limits_and_bad_source_actions(uploads, media_samples, monkeypatch):
    u = uploads
    with pytest.raises(AppException) as error:
        await u.service.create(u.user.id, "huge.mp3", 257 * 1024**2)
    assert error.value.code == "FILE_TOO_LARGE"
    monkeypatch.setattr(settings, "UPLOAD_MAX_PENDING_PER_USER", 1)
    pending = await u.service.create(u.user.id, "small.mp3", 10)
    with pytest.raises(AppException) as error:
        await u.service.create(u.user.id, "other.mp3", 10)
    assert error.value.code == "TOO_MANY_UPLOADS"
    with pytest.raises(AppException) as error:
        await u.billing.quote(u.user.id, None, "ai_summary", "audio_mp3", "zh", upload_id=pending["id"])
    assert error.value.code == "UPLOAD_NOT_READY"
    await u.service.delete(pending["id"], u.user.id)
    item = await ready(u, media_samples["sample.mp3"])
    with pytest.raises(AppException) as error:
        await u.billing.quote(u.user.id, None, "download", "video_720_mp4", "zh", upload_id=item["id"])
    assert error.value.code == "UPLOAD_ACTION_UNSUPPORTED"
    with pytest.raises(AppException) as error:
        await u.service.resolve(item["id"], None)
    assert error.value.code == "AUTH_REQUIRED"
    monkeypatch.setattr(settings, "UPLOAD_MAX_PER_HOUR", 1)
    with pytest.raises(AppException) as error:
        await u.service.create(u.user.id, "third.mp3", 10)
    assert error.value.code == "UPLOAD_RATE_LIMITED"


async def test_probe_cancel_at_completion_cannot_leave_ready_source(uploads, media_samples):
    u = uploads
    item = await received(u, media_samples["sample.mp3"])
    created = await u.service.complete(item["id"], u.user.id)
    async def inspect(*args, **kwargs):
        u.repository.request_cancel(created["task_id"])
        return {"probe": {"format": {"format_name": "mp3", "duration": "10"},
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}]}}
    outcome = await execute_probe(created["task_id"], u.tasks, u.service, SimpleNamespace(inspect=inspect))
    assert outcome["status"] == "cancelled"
    assert (await u.service.get(item["id"], u.user.id))["status"] == "failed"
    with u.repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Video)) == 0
    assert not u.service.files.path(item["id"]).exists()


async def test_confirm_rechecks_deleted_source_but_existing_confirmation_is_idempotent(uploads, media_samples):
    u = uploads
    item = await ready(u, media_samples["sample.mp3"])
    quote = await u.billing.quote(u.user.id, None, "transcription", "audio_mp3", "zh", upload_id=item["id"])
    await u.service.delete(item["id"], u.user.id)
    with pytest.raises(AppException) as error:
        await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    assert error.value.code == "UPLOAD_EXPIRED"
    item = await ready(u, media_samples["sample.mp3"])
    quote = await u.billing.quote(u.user.id, None, "transcription", "audio_mp3", "zh", upload_id=item["id"])
    first = await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))
    u.repository.request_cancel(first.id)
    await u.service.delete(item["id"], u.user.id)
    assert (await u.billing.confirm(u.user.id, UUID(quote["quote_id"])) ).id == first.id


async def test_lost_receive_ack_does_not_delete_committed_bytes(uploads, media_samples, monkeypatch):
    u = uploads
    content = media_samples["sample.mp3"]
    item = await u.service.create(u.user.id, "sample.mp3", len(content))
    original = u.service.repository.received
    def committed_then_lost(*args):
        original(*args)
        raise AppException("DATABASE_UNAVAILABLE", "提交结果未知", 503)
    monkeypatch.setattr(u.service.repository, "received", committed_then_lost)
    with pytest.raises(AppException):
        await u.service.receive(item["id"], u.user.id, chunks(content))
    assert (await u.service.get(item["id"], u.user.id))["status"] == "uploaded"
    assert u.service.files.path(item["id"]).read_bytes() == content
    created = await u.service.complete(item["id"], u.user.id)
    outcome = await execute_probe(created["task_id"], u.tasks, u.service, FFprobeAdapter(ProcessRunner(10, 1024**2)))
    assert outcome["status"] == "completed"


async def test_probe_duration_limit_fails_before_any_asr_or_charge(uploads, media_samples):
    u = uploads
    before = u.billing.repository.balance(u.user.id)["balances"]
    item = await received(u, media_samples["sample.mp3"])
    created = await u.service.complete(item["id"], u.user.id)
    probe = Mock(inspect=AsyncMock(return_value={"probe": {"format": {"format_name": "mp3", "duration": "1201"},
        "streams": [{"codec_type": "audio", "codec_name": "mp3"}]}}))
    outcome = await execute_probe(created["task_id"], u.tasks, u.service, probe)
    assert outcome["status"] == "failed"
    assert u.repository.get(created["task_id"]).error_code == "VIDEO_TOO_LONG"
    assert u.billing.repository.balance(u.user.id)["balances"] == before


async def test_cleanup_cannot_remove_public_audio_pinned_by_transcription(repository, storage, monkeypatch):
    from app.modules.video.parsers.base import ParsedVideo
    from app.modules.media.models import MediaFile
    from test_media_and_transcript import seed_media
    item = seed_media(repository, storage, ParsedVideo("youtube", "pinned", "fixture", duration=1, formats=[]))
    path = storage.resolve_file(item.storage_key)
    with repository.sessions.begin() as session:
        session.execute(update(MediaFile).where(MediaFile.id == item.id).values(expires_at=settings_date()))
    with storage.file_lock(path):
        counts = await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository)
        assert counts["files"] == 0 and path.exists()
    counts = await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository)
    assert counts["files"] == 1 and not path.exists()


def test_m4a_cover_art_is_not_treated_as_video():
    from app.modules.uploads.probe import validate_probe
    info = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1"}, "streams": [
        {"codec_type": "audio", "codec_name": "aac", "duration": "1"},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}]}
    assert validate_probe(info, "m4a") == {"duration_seconds": 1, "media_type": "audio"}
