import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import wave

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure import timing
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.http_client import PublicNetworkBackend
from app.infrastructure.timeouts import operation_timeout
from app.modules.ai.providers.base import LLMRequest
from app.modules.ai.providers.openai_compatible import OpenAICompatibleLLMProvider
from app.modules.tasks.diagnostics import TIMEOUT_FIELDS, timeout_report
from app.modules.transcript import providers as asr
from app.modules.transcript.worker import cooperative_asr


def request():
    return LLMRequest("summary", {}, {}, "zh", "subtitle")


def llm_response():
    return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})


async def test_parent_deadline_wins_and_child_cleanup_finishes():
    cleaned = asyncio.Event()
    with pytest.raises(AppException) as error:
        async with operation_timeout("ai_task", .03, "AI_TIMEOUT", "overall") as parent:
            async with operation_timeout("asr", 10, "ASR_TIMEOUT", "stage") as child:
                assert child.expires_at == parent.expires_at
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
    assert error.value.code == "AI_TIMEOUT" and cleaned.is_set()


async def test_child_deadline_is_distinct_and_scope_does_not_leak():
    async with operation_timeout("task", 1, "AI_TIMEOUT", "overall") as parent:
        with pytest.raises(AppException) as error:
            async with operation_timeout("asr", .02, "ASR_TIMEOUT", "stage"):
                await asyncio.Event().wait()
        assert error.value.code == "ASR_TIMEOUT"
        async with operation_timeout("next", 10, "LLM_TIMEOUT", "request") as next_budget:
            assert next_budget.expires_at == parent.expires_at
    async with operation_timeout("other_task", 10, "AI_TIMEOUT", "other") as other:
        assert other.expires_at > parent.expires_at


async def test_external_cancellation_is_not_reported_as_a_timeout():
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def run():
        async with operation_timeout("request", 100, "LLM_TIMEOUT", "request"):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_unrelated_timeout_error_is_not_relabelled():
    original = TimeoutError("not this deadline")
    with pytest.raises(TimeoutError) as error:
        async with operation_timeout("request", 10, "LLM_TIMEOUT", "request"):
            raise original
    assert error.value is original


async def test_http_read_and_write_budgets_use_configuration_and_parent_remaining(monkeypatch):
    monkeypatch.setattr(settings, "CLOUD_READ_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(settings, "CLOUD_WRITE_TIMEOUT_SECONDS", 120)
    calls = []

    def handle(req):
        calls.append(req.extensions["timeout"])
        return llm_response()

    provider = OpenAICompatibleLLMProvider("https://llm.example/v1", "secret", "model",
        timeout=180, transport=httpx.MockTransport(handle))
    await provider.generate(request())
    assert calls[0]["read"] == calls[0]["write"] == 120
    async with operation_timeout("parent", .5, "AI_TIMEOUT", "overall"):
        await provider.generate(request())
    assert all(0 < value <= .5 for value in calls[1].values())


async def test_dns_and_connect_share_the_configured_connect_budget(monkeypatch):
    monkeypatch.setattr(settings, "CLOUD_CONNECT_TIMEOUT_SECONDS", .1)

    async def resolve(*args, **kwargs):
        await asyncio.sleep(.02)
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    backend = SimpleNamespace(connect_tcp=AsyncMock(return_value=object()))
    await PublicNetworkBackend(backend).connect_tcp("asr.example", 443, timeout=1)
    assert backend.connect_tcp.call_args.args[0] == "93.184.216.34"
    assert 0 < backend.connect_tcp.call_args.args[2] < .09


async def test_retry_backoff_cannot_reset_llm_request_budget():
    calls = []

    def handle(req):
        calls.append(req)
        return httpx.Response(503)

    provider = OpenAICompatibleLLMProvider("https://llm.example/v1", "secret", "model",
        timeout=.04, transport=httpx.MockTransport(handle))
    with pytest.raises(AppException) as error:
        await asyncio.wait_for(provider.generate(request()), 1)
    assert error.value.code == "LLM_TIMEOUT" and len(calls) == 1


@pytest.mark.parametrize("provider_class,method", [(asr.OpenAICompatibleASRProvider, "transcribe"),
                                                  (asr.QwenASRProvider, "_transcribe_chunk")])
async def test_asr_per_request_deadline_is_separate_from_stage(tmp_path, monkeypatch, provider_class, method):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"audio")
    monkeypatch.setattr(settings, "ASR_REQUEST_TIMEOUT_SECONDS", .03)
    cleaned = asyncio.Event()

    async def slow(req):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    provider = provider_class("https://asr.example/v1", "secret", "model", timeout=1,
                              transport=httpx.MockTransport(slow))
    with pytest.raises(AppException) as error:
        await asyncio.wait_for(getattr(provider, method)(path, "zh"), .5)
    assert error.value.code == "ASR_TIMEOUT" and cleaned.is_set()


def write_chunk(directory, index):
    with wave.open(str(directory / f"chunk-{index:05d}.wav"), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 16000)


async def test_qwen_all_chunks_have_stage_budget_not_one_request_budget(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")
    monkeypatch.setattr(settings, "ASR_REQUEST_TIMEOUT_SECONDS", .2)
    monkeypatch.setattr(settings, "ASR_CHUNK_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "ASR_PREPROCESS_TIMEOUT_SECONDS", 150)
    monkeypatch.setattr(asr.shutil, "which", lambda _: "/opt/ffmpeg")

    async def split(runner, command, directory, **controls):
        # Use a long stage budget here to verify removal of the hidden 30s cap.
        assert 30 < runner.timeout <= 150
        write_chunk(directory, 0)
        write_chunk(directory, 1)

    monkeypatch.setattr(asr.ProcessRunner, "run", split)
    requests = []

    async def respond(req):
        requests.append(req)
        await asyncio.sleep(.12)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"文字{len(requests)}"}}]})

    provider = asr.QwenASRProvider("https://asr.example/v1", "secret", "qwen3-asr-flash",
                                 timeout=300, transport=httpx.MockTransport(respond))
    data = await provider.transcribe(source, "zh")
    assert len(requests) == 2 and len(data.segments) == 2
    assert data.segments[-1]["end"] == 2
    assert source.read_bytes() == b"source" and not list(tmp_path.glob("asr-chunks-*"))


async def test_preprocessing_timeout_is_specific_and_cleans_files(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")
    monkeypatch.setattr(settings, "ASR_PREPROCESS_TIMEOUT_SECONDS", .03)
    monkeypatch.setattr(asr.shutil, "which", lambda _: "/opt/ffmpeg")

    async def split(runner, command, directory, **controls):
        (directory / "partial.wav").write_bytes(b"partial")
        await asyncio.Event().wait()

    monkeypatch.setattr(asr.ProcessRunner, "run", split)
    provider = asr.QwenASRProvider("https://asr.example/v1", "secret", "qwen3-asr-flash", timeout=1)
    provider._transcribe_chunk = AsyncMock()
    with pytest.raises(AppException) as error:
        await provider.transcribe(source, "zh")
    assert error.value.code == "AUDIO_PREPROCESS_TIMEOUT"
    provider._transcribe_chunk.assert_not_awaited()
    assert not list(tmp_path.glob("asr-chunks-*")) and source.exists()


async def test_cooperative_asr_children_inherit_overall_deadline_and_are_awaited(monkeypatch):
    monkeypatch.setattr(settings, "ASR_TOTAL_TIMEOUT_SECONDS", 10)
    cleaned = asyncio.Event()

    async def transcribe(*args):
        async with operation_timeout("request", 100, "ASR_TIMEOUT", "request"):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    with pytest.raises(AppException) as error:
        async with operation_timeout("ai_task", .03, "AI_TIMEOUT", "overall"):
            await cooperative_asr(SimpleNamespace(transcribe=transcribe), None, None, AsyncMock(return_value=False))
    assert error.value.code == "AI_TIMEOUT" and cleaned.is_set()


async def test_timeout_logs_budget_stage_and_task_without_credentials(caplog, monkeypatch):
    monkeypatch.setattr(timing.logger, "disabled", False)
    task_id = uuid4()
    with caplog.at_level("INFO", logger=timing.logger.name):
        with pytest.raises(AppException):
            async with operation_timeout("ai_task", .03, "AI_TIMEOUT", "overall", task_id):
                async with operation_timeout("llm_request", 10, "LLM_TIMEOUT", "request"):
                    await asyncio.Event().wait()
    assert "stage=llm_request" in caplog.text and f"task_id={task_id}" in caplog.text
    assert "budget_ms=" in caplog.text and "error_code=AI_TIMEOUT" in caplog.text


async def test_deadline_stops_real_process_group_before_removing_work_directory(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    marker = tmp_path / "pid"
    child = "import time; time.sleep(30)"
    script = ("import os, sys, time, subprocess; from pathlib import Path; "
              f"p = subprocess.Popen([sys.executable, '-c', {child!r}]); "
              f"Path({str(marker)!r}).write_text(str(os.getpid()) + ',' + str(p.pid)); time.sleep(30)")
    runner = ProcessRunner(10, 1024 * 1024, interval=.05, term_grace=.2)
    with pytest.raises(AppException) as error:
        async with operation_timeout("asr_preprocess", .5, "AUDIO_PREPROCESS_TIMEOUT", "preprocess"):
            await runner.run([sys.executable, "-c", script], directory, cwd=tmp_path,
                is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
    assert error.value.code == "AUDIO_PREPROCESS_TIMEOUT" and marker.exists()
    pid, child_pid = map(int, marker.read_text().split(','))
    for stopped in (pid, child_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(stopped, 0)
    assert not runner.group_exists(pid) and not directory.exists()


def test_diagnostics_only_reports_allowed_fields_and_sanitized_task(monkeypatch):
    monkeypatch.setattr(settings, "AI_TASK_TIMEOUT_SECONDS", 900)
    monkeypatch.setattr(settings, "ASR_TOTAL_TIMEOUT_SECONDS", 1800)
    started = datetime.now(timezone.utc)
    task = SimpleNamespace(id=uuid4(), task_type="ai_summary", status="failed", progress=50,
        error_code="ASR_TIMEOUT", started_at=started, finished_at=started + timedelta(seconds=15),
        input_payload={"url": "https://user:password@example.com/?secret=value"}, error_message="private input")
    report = timeout_report(task)
    assert set(report["effective_seconds"]) == set(TIMEOUT_FIELDS)
    assert report["notes"] and report["task"]["duration_seconds"] == 15
    encoded = json.dumps(report)
    assert "password" not in encoded and "private input" not in encoded and "example.com" not in encoded
