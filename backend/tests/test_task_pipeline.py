"""Exercise phase boundaries, cache reuse, and crash recovery on task contracts."""
from dataclasses import replace
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import wave

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.ai.providers.base import LLMRequest, LLMResult
from app.modules.ai.providers.checkpointed import CheckpointedLLMProvider
from app.modules.billing.manage import recover
from app.modules.download.adapter import YtDlpDownloadAdapter
from app.modules.tasks.checkpoints import Checkpoints, active_checkpoints
from app.modules.tasks.dispatch import enqueue
from app.modules.tasks.execution import execution_lock
from app.modules.tasks.pipeline import run_phase
from app.modules.tasks.service import TaskService
from app.modules.transcript import providers
from app.modules.transcript.repository import TranscriptRepository
from test_allowances import billing
from test_download import storage, video
from test_media_and_transcript import task_for
from test_tasks import MemoryBus, repository


@pytest.fixture(autouse=True)
def enable_stages(monkeypatch):
    monkeypatch.setattr(settings, "STAGED_TASKS_ENABLED", True)


def write_audio(path):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)


async def test_download_render_handoff_and_versioned_result_reuse(repository, storage, video, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is required for staged media rendering")
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 32)
    source = storage.root / "sample.mp3"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    str(source)], check=True, timeout=10)
    service = TaskService(repository, MemoryBus())
    broker = []

    async def transfer(adapter, payload, directory, **controls):
        assert payload["_defer_render"]
        shutil.copyfile(source, directory / "source_0.mp3")
        manifest = directory / "download-sources.json"
        manifest.write_text('{"sources":["source_0.mp3"],"codecs":[{"acodec":"mp3"}]}')
        return manifest

    mocked = AsyncMock(side_effect=transfer)
    async def fixture_transfer(self, *args, **kwargs):
        return await mocked(self, *args, **kwargs)
    monkeypatch.setattr(YtDlpDownloadAdapter, "download", fixture_transfer)
    dispatch = lambda repo, task, _: broker.append((task.id, task.phase))
    first = task_for(repository, video, "download")
    assert await run_phase(first.id, "download", service, storage, dispatch=dispatch) == "processing"
    assert broker == [(first.id, "render")]
    assert not list((storage.root / "files").iterdir())
    assert await run_phase(first.id, "download", service, storage, dispatch=dispatch) == "processing"
    assert await run_phase(*broker.pop(), service, storage, dispatch=dispatch) == "completed"
    second = task_for(repository, video, "download")
    assert await run_phase(second.id, "download", service, storage, dispatch=dispatch) == "completed"
    a, b = repository.get(first.id), repository.get(second.id)
    assert a.result_payload["file_id"] == b.result_payload["file_id"]
    assert b.result_payload["reused"] is True
    assert mocked.await_count == 1
    assert not storage.task_directory(first.id).exists()


async def test_prepare_asr_and_llm_keep_one_parent(repository, storage, video, monkeypatch):
    from app.modules.media.audio import AudioAcquirer
    from app.modules.transcript.subtitles import YtDlpSubtitleProvider
    from app.modules.transcript.data import make_transcript
    from app.modules.ai.providers.heuristic import HeuristicLLMProvider

    async def acquire(self, payload, directory, **controls):
        path = directory / "sample.wav"
        write_audio(path)
        return path

    calls = []

    async def transcribe(path, language):
        assert path.name == "prepared.wav"
        calls.append(path.name)
        return make_transcript("zh", [{"start": 0, "end": 1, "text": "同一个任务应保持原有进度。"}])

    monkeypatch.setattr(AudioAcquirer, "acquire", acquire)
    monkeypatch.setattr(YtDlpSubtitleProvider, "fetch", AsyncMock(return_value=None))
    monkeypatch.setattr(providers, "get_asr_provider",
                        lambda: SimpleNamespace(model="offline-test", transcribe=transcribe))
    monkeypatch.setattr("app.modules.ai.providers.get_llm_provider", HeuristicLLMProvider)
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe required")
    task = task_for(repository, video, "ai_summary", output_language="zh")
    service = TaskService(repository, MemoryBus())
    broker = []
    dispatch = lambda repo, row, _: broker.append((row.id, row.phase))
    assert await run_phase(task.id, "prepare", service, storage, dispatch=dispatch) == "processing"
    assert not calls and broker == [(task.id, "asr")]
    assert await run_phase(*broker.pop(), service, storage, dispatch=dispatch) == "processing"
    assert calls == ["prepared.wav"] and broker == [(task.id, "llm")]
    assert await run_phase(*broker.pop(), service, storage, dispatch=dispatch) == "completed"
    completed = repository.get(task.id)
    assert completed.attempts == 1
    transcript = TranscriptRepository(repository.sessions).get(completed.prepared_transcript_id)
    assert transcript.task_id == task.id
    assert completed.result_payload["transcript_id"] == str(transcript.id)
    assert not storage.task_directory(task.id).exists()


async def test_handoff_recovery_replays_only_current_phase(repository, storage, video, monkeypatch):
    task = task_for(repository, video, "ai_summary")
    id1 = str(uuid4())
    repository.claim_phase(task.id, "prepare", id1)
    with execution_lock(storage.root, task.id):
        assert recover(repository, storage.root) == 0
    assert recover(repository, storage.root) == 1
    current = repository.get(task.id)
    assert current.phase == "prepare" and current.worker_pid is None and current.attempts == 1
    id2 = str(uuid4())
    assert repository.claim_phase(task.id, "prepare", id2)
    repository.handoff_phase(task.id, id2, "prepare", "asr", 20)
    assert not repository.claim_phase(task.id, "prepare", id1)
    assert repository.finish(task.id, "failed", 20, expected_execution_id=id2).status == "processing"

    sent = []
    monkeypatch.setattr("app.modules.tasks.dispatch.celery_app.send_task", lambda *args, **kwargs: sent.append(kwargs))
    row = repository.queued(dispatch=True)[0]
    assert enqueue(repository, row, None)
    assert not enqueue(repository, row, None)
    assert sent == [{"args": [str(task.id), "asr"], "queue": "asr"}]
    assert repository.request_cancel(task.id).status == "cancelled"
    assert not repository.claim_phase(task.id, "asr", str(uuid4()))


def test_account_slots_and_phase_handoff_do_not_hold_twice(billing):
    first, second, third = (billing.create() for _ in range(3))
    owner = str(uuid4())
    assert billing.tasks.claim_phase(first.id, "prepare", owner)
    assert billing.tasks.claim_phase(second.id, "prepare", str(uuid4()))
    assert not billing.tasks.claim_phase(third.id, "prepare", str(uuid4()))
    billing.tasks.handoff_phase(first.id, owner, "prepare", "asr", 20)
    assert billing.tasks.claim_phase(first.id, "asr", str(uuid4()))
    balance = billing.quotas.balance(billing.user.id)["balances"]["ai_generations"]
    assert balance["held"] == 2
    billing.tasks.complete_with(first.id, {}, lambda session: None)
    billing.tasks.complete_with(first.id, {}, lambda session: None)
    balance = billing.quotas.balance(billing.user.id)["balances"]["ai_generations"]
    assert balance["available"] == 3 and balance["held"] == 1
    assert billing.tasks.claim_phase(third.id, "prepare", str(uuid4()))


async def test_qwen_retry_reuses_only_successful_chunks_for_same_owner(repository, video, tmp_path):
    first = task_for(repository, video)
    owner = uuid4()
    source = tmp_path / "piece.wav"
    write_audio(source)
    other = tmp_path / "other.wav"
    write_audio(other)
    with other.open("ab") as output:
        output.write(b"changed")
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(401)
        return httpx.Response(200, json={"choices": [{"message": {"content": "片段"}}]})

    provider = providers.QwenASRProvider("https://example.com", "test", "qwen3-asr-flash",
                                         transport=httpx.MockTransport(respond))
    token = active_checkpoints.set(Checkpoints(repository.sessions, replace(first, user_id=owner)))
    try:
        await provider._transcribe_chunk(source, "zh")
        with pytest.raises(AppException):
            await provider._transcribe_chunk(other, "zh")
    finally:
        active_checkpoints.reset(token)
    for user in (owner, uuid4()):
        retry = replace(first, id=uuid4(), user_id=user)
        token = active_checkpoints.set(Checkpoints(repository.sessions, retry))
        try:
            await provider._transcribe_chunk(source, "zh")
            await provider._transcribe_chunk(other, "zh")
        finally:
            active_checkpoints.reset(token)
    assert len(calls) == 5


async def test_llm_checkpoint_is_scoped_to_request_and_prompt_version(repository, video):
    task = task_for(repository, video)
    reply = LLMResult({"summary": "已有结果"}, 10, 5, 20, "model")
    provider = SimpleNamespace(name="fixture", model="model", generate=AsyncMock(return_value=reply))
    req = LLMRequest("summary", {"text": "输入"}, {}, "zh", "subtitle")
    cache = Checkpoints(repository.sessions, task)
    assert await CheckpointedLLMProvider(provider, cache, "hash", ["v1"]).generate(req) == reply
    replay = await CheckpointedLLMProvider(provider, cache, "hash", ["v1"]).generate(req)
    assert replay == replace(reply, duration_ms=0)
    await CheckpointedLLMProvider(provider, cache, "hash", ["v2"]).generate(req)
    assert provider.generate.await_count == 2


async def test_distinct_download_tasks_overlap_with_text_phase(repository, storage, video):
    import asyncio

    tasks = [
        task_for(repository, video, "download"),
        task_for(repository, video, "download"),
        task_for(repository, video, "ai_summary", source_kind="transcript"),
    ]
    ready = asyncio.Event()
    active = set()

    async def execute(service, stage_storage, task, execution_id):
        active.add(task.id)
        if len(active) == 3:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        from app.modules.tasks.service import database_call
        await database_call(service.repository.complete_with, task.id, {}, lambda session: None)

    states = await asyncio.gather(*(
        run_phase(task.id, task.phase, TaskService(repository, MemoryBus()), storage, body=execute)
        for task in tasks
    ))
    assert states == ["completed"] * 3 and active == {task.id for task in tasks}


async def test_long_compatible_asr_keeps_segment_offsets_and_checkpoints(repository, video, tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_audio(source)
    task = task_for(repository, video)
    requests = []

    async def split(ffmpeg, path, directory, *args):
        for index in range(2):
            file = directory / f"chunk-{index:05d}.wav"
            write_audio(file)
            with file.open("ab") as output:
                output.write(bytes([index]))

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"segments": [
            {"start": .1, "end": .7, "text": f"时间轴 {len(requests)}"},
        ]})

    monkeypatch.setattr(providers, "_split_audio", split)
    monkeypatch.setattr(providers.shutil, "which", lambda _: "/opt/ffmpeg")
    token = active_checkpoints.set(Checkpoints(repository.sessions, task))
    duration = providers.asr_audio_duration.set(121)
    try:
        provider = providers.OpenAICompatibleASRProvider(
            "https://example.com/v1", "fixture", "fixture", transport=httpx.MockTransport(respond))
        first = await provider.transcribe(source, "zh")
        assert await provider.transcribe(source, "zh") == first
        assert len(requests) == 2
        assert [item["start"] for item in first.segments] == [.1, 1.1]
    finally:
        providers.asr_audio_duration.reset(duration)
        active_checkpoints.reset(token)
