"""R1 positive regressions: process ownership, recovery, cleanup and speech boundaries."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from test_clips import (clips, uploads, repository, storage, media_samples, clip_video,
                        Selector, Timing, plan_task, TEXTS)
from app.core.exceptions import AppException
from app.infrastructure.process_control import FileProcessControl, atomic_json, guarded_temporary_directory
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.modules.billing.manage import recover
from app.modules.clips.schemas import PlanQuoteRequest, ClipStrategy, ExportQuoteRequest
from app.modules.clips.timing import playback_ranges
from app.modules.clips.worker import execute_plan, execute_export, prepare_timeline
from app.modules.tasks.execution import receipt_path, run_owned
from app.modules.tasks.models import Task
from app.modules.transcript.data import make_transcript
from app.modules.transcript.providers import QwenASRProvider


@contextmanager
def native_group():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


async def quoted(u, kind="plan"):
    if kind == "plan":
        quote = await u.clips.quote_plan(u.user, PlanQuoteRequest(transcript_id=u.transcript.id,
            strategy=ClipStrategy(min_seconds=1, max_seconds=10)))
    else:
        _, plan, _ = await plan_task(u)
        quote = await u.clips.quote_export(u.user, ExportQuoteRequest(plan_id=plan["id"],
            candidate_ids=[plan["candidates"][0]["id"]]))
    return await u.billing.confirm(u.user.id, UUID(quote["quote_id"]))


@pytest.mark.parametrize("kind", ["plan", "export"])
async def test_bootstrap_read_failure_settles_once_without_model_calls(clips, monkeypatch, kind):
    u = clips
    task = await quoted(u, kind)
    before = u.billing.repository.balance(u.user.id)["balances"]
    original = u.repository.get
    def unavailable(*args):
        raise AppException("DATABASE_UNAVAILABLE", "injected initial read failure", 503)
    monkeypatch.setattr(u.repository, "get", unavailable)
    provider = Selector() if kind == "plan" else Timing()
    function = execute_plan if kind == "plan" else execute_export
    result = await function(task.id, u.clips, provider=provider)
    monkeypatch.setattr(u.repository, "get", original)
    assert result["status"] == "failed" and original(task.id).error_code == "DATABASE_UNAVAILABLE"
    assert provider.calls == 0
    assert u.billing.repository.balance(u.user.id)["balances"] == before
    assert (await function(task.id, u.clips, provider=provider))["status"] == "failed"
    assert u.billing.repository.balance(u.user.id)["balances"] == before


async def test_start_ack_lost_uses_owned_cas_and_releases_allowance(clips, monkeypatch):
    u = clips
    task = await quoted(u)
    original = u.repository.start
    def lost_reply(*args, **kwargs):
        assert original(*args, **kwargs)
        raise AppException("DATABASE_UNAVAILABLE", "start committed but reply lost", 503)
    monkeypatch.setattr(u.repository, "start", lost_reply)
    provider = Selector()
    assert (await execute_plan(task.id, u.clips, provider=provider))["status"] == "failed"
    assert provider.calls == 0
    assert u.billing.repository.balance(u.user.id)["balances"]["ai_generations"]["held"] == 0


async def test_start_failure_cannot_settle_a_different_execution(clips, monkeypatch):
    u = clips
    task = await quoted(u)
    original = u.repository.start
    other = str(uuid4())
    def other_owner(*args, **kwargs):
        assert original(task.id, execution_id=other)
        raise AppException("DATABASE_UNAVAILABLE", "our claim failed", 503)
    monkeypatch.setattr(u.repository, "start", other_owner)
    provider = Selector()
    assert (await execute_plan(task.id, u.clips, provider=provider))["status"] == "running"
    assert provider.calls == 0
    assert u.repository.get(task.id).input_payload["_execution_id"] == other
    assert u.billing.repository.balance(u.user.id)["balances"]["ai_generations"]["held"] == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_stopped_attempt_recovers_with_worker_pid_still_alive(clips, monkeypatch, cancel):
    u = clips
    task = await quoted(u)
    original_get, original_finish = u.repository.get, u.repository.finish
    def unavailable(*args, **kwargs):
        raise AppException("DATABASE_UNAVAILABLE", "injected persistent DB failure", 503)
    monkeypatch.setattr(u.repository, "get", unavailable)
    monkeypatch.setattr(u.repository, "finish", unavailable)
    with pytest.raises(AppException):
        await execute_plan(task.id, u.clips, provider=Selector())
    monkeypatch.setattr(u.repository, "get", original_get)
    monkeypatch.setattr(u.repository, "finish", original_finish)
    current = original_get(task.id)
    assert current.worker_pid == os.getpid() and current.status == "running"
    path = receipt_path(u.storage.root, task.id, current.input_payload["_execution_id"])
    assert json.loads(path.read_text())["state"] == "stopped"
    if cancel:
        u.repository.request_cancel(task.id)
    assert recover(u.repository, u.storage.root) == 1
    assert original_get(task.id).status == ("cancelled" if cancel else "failed")
    assert u.billing.repository.balance(u.user.id)["balances"]["ai_generations"]["held"] == 0
    assert recover(u.repository, u.storage.root) == 0 and not path.exists()


async def test_duplicate_and_reconcile_do_not_interrupt_active_attempt(clips):
    u = clips
    task = await quoted(u)
    entered, release = asyncio.Event(), asyncio.Event()
    async def body(current):
        entered.set()
        await release.wait()
        done = u.repository.finish(current.id, "failed", 0)
        return {"task_id": str(done.id), "status": done.status}
    operation = asyncio.create_task(run_owned(task.id, u.clips, body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = await run_owned(task.id, u.clips, lambda _: pytest.fail("duplicate executed body"))
        assert duplicate["status"] == "running"
        assert recover(u.repository, u.storage.root) == 0
        assert u.billing.repository.balance(u.user.id)["balances"]["ai_generations"]["held"] == 1
    finally:
        release.set()
        await operation


async def test_execution_tokens_are_not_exposed_in_task_api(clips):
    u = clips
    task = await quoted(u)
    assert u.repository.start(task.id, execution_id=str(uuid4()))
    assert "_execution_id" not in (await u.tasks.get(task.id))["input_payload"]


async def test_lost_failure_reply_is_later_cleaned_without_double_settlement(clips, monkeypatch):
    u = clips
    task = await quoted(u)
    original_get, original_finish = u.repository.get, u.repository.finish
    def failed_read(*args):
        raise AppException("DATABASE_UNAVAILABLE", "read unavailable", 503)
    def lost_finish_reply(*args, **kwargs):
        original_finish(*args, **kwargs)
        raise AppException("DATABASE_UNAVAILABLE", "failure committed but reply lost", 503)
    monkeypatch.setattr(u.repository, "get", failed_read)
    monkeypatch.setattr(u.repository, "finish", lost_finish_reply)
    with pytest.raises(AppException):
        await execute_plan(task.id, u.clips, provider=Selector())
    monkeypatch.setattr(u.repository, "get", original_get)
    monkeypatch.setattr(u.repository, "finish", original_finish)
    current = original_get(task.id)
    path = receipt_path(u.storage.root, task.id, current.input_payload["_execution_id"])
    assert current.status == "failed" and path.exists()
    before = u.billing.repository.balance(u.user.id)["balances"]
    await cleanup_expired_files(u.storage, u.clips.media, u.repository)
    assert not path.exists() and u.billing.repository.balance(u.user.id)["balances"] == before


async def test_nested_legacy_process_blocks_recovery_cleanup_and_source_deletion(clips):
    u = clips
    task = await quoted(u)
    assert u.repository.start(task.id)
    with u.repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(worker_pid=99999999,
            input_payload={**task.input_payload, "upload_id": str(u.upload_id)}))
    directory = u.storage.create_task_directory(task.id)
    nested = directory / "asr-chunks-old"
    nested.mkdir()
    with native_group() as process:
        atomic_json(nested / ".process.json", {"pgid": process.pid})
        media_key = f"files/{uuid4()}.mp4"
        u.storage.resolve_file(media_key).write_bytes(b"pending")
        atomic_json(directory / ".clip-commit.json", {"files": [media_key]})
        assert not ProcessRunner.directory_stopped(directory)
        assert recover(u.repository, u.storage.root) == 0
        assert await u.service.in_use(u.upload_id)
        # A terminal task still cannot let the independent process lose its working files.
        u.repository.finish(task.id, "failed", 0)
        await cleanup_expired_files(u.storage, u.clips.media, u.repository)
        assert nested.exists()
        with pytest.raises(AppException):
            u.storage.discard_batch(task.id, set())
        assert u.storage.resolve_file(media_key).exists()
        with pytest.raises(AppException) as error:
            u.storage.remove_task_directory(task.id)
        assert error.value.code == "PROCESS_CLEANUP_FAILED"
    assert ProcessRunner.directory_stopped(directory)
    await cleanup_expired_files(u.storage, u.clips.media, u.repository)
    assert not directory.exists()


async def test_recovery_waits_for_all_groups_not_just_the_last(clips):
    u = clips
    task = await quoted(u)
    assert u.repository.start(task.id)
    with u.repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(worker_pid=99999999))
    directory = u.storage.create_task_directory(task.id)
    registry = FileProcessControl()
    with native_group() as first, native_group() as second:
        a = registry.prepare(directory)
        registry.started(a, first.pid, directory)
        sub = directory / "other"
        sub.mkdir()
        b = registry.prepare(sub)
        registry.started(b, second.pid, sub)
        os.killpg(second.pid, signal.SIGTERM)
        second.wait(timeout=5)
        assert recover(u.repository, u.storage.root) == 0 and directory.exists()
    assert recover(u.repository, u.storage.root) == 1
    assert not directory.exists()


async def test_runner_registers_nested_process_at_task_root(storage):
    directory = storage.create_task_directory(uuid4())
    sub = directory / "asr" / "nested"
    sub.mkdir(parents=True)
    result = await ProcessRunner(5, 1024**2, interval=.01).run(
        [sys.executable, "-c", 'print(\'{"ok":true}\')'], sub, cwd=sub,
        is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
    assert result == {"ok": True}
    records = list((directory / ".operations").glob("*.json"))
    assert len(records) == 1
    receipt = json.loads(records[0].read_text())
    assert receipt["task_id"] == directory.name and receipt["work_directory"] == "asr/nested"
    assert receipt["state"] == "exited" and ProcessRunner.directory_stopped(directory)


async def test_native_payload_limit_excludes_receipts_but_storage_quota_counts_them(storage):
    directory = storage.create_task_directory(uuid4())
    result = await ProcessRunner(5, 100, interval=.01).run(
        [sys.executable, "-c", 'print(\'{"ok":true}\')'], directory, cwd=directory,
        is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
    from app.infrastructure.process_runner import directory_size
    assert result["ok"] and directory_size(directory, include_control_files=False) == 0
    assert directory_size(directory) > 100 and storage.used_bytes() >= directory_size(directory)


def test_render_day_boundary_uses_same_utc_instant_in_sqlite(clips, monkeypatch):
    from datetime import datetime, timezone
    import app.modules.tasks.repository as task_repository
    u = clips
    # 00:30 Beijing, while the corresponding UTC date is still the previous day.
    moment = datetime(2026, 11, 1, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(task_repository, "utcnow", lambda: moment)
    task = u.repository.create("simulate_progress")
    plan_id = str(uuid4())
    with u.repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(user_id=u.user.id,
            task_type="clips_export", started_at=moment, input_payload={"plan_id": plan_id}))
    with u.repository.sessions() as session:
        assert u.repository.clip_render_counts(session, u.user.id, plan_id) == (1, 1)


async def test_unconfirmed_native_stop_preserves_directory_and_reservation(repository, storage, monkeypatch):
    task = repository.create("simulate_progress")
    assert repository.start(task.id)
    processes = []
    actual_popen = subprocess.Popen
    def tracked(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr("app.infrastructure.process_runner.subprocess.Popen", tracked)
    runner = ProcessRunner(.2, 1024**2, interval=.01)
    async def unconfirmed(process):
        raise AppException("PROCESS_CLEANUP_FAILED", "injected unconfirmed native stop", 500)
    monkeypatch.setattr(runner, "stop_group", unconfirmed)
    try:
        with storage.reservation(task.id, 1024**2):
            directory = storage.create_task_directory(task.id)
            with pytest.raises(AppException) as error:
                await runner.run([sys.executable, "-c", "import time; time.sleep(30)"], directory, cwd=directory,
                    is_cancel_requested=AsyncMock(return_value=False), on_progress=AsyncMock())
            assert error.value.code == "PROCESS_CLEANUP_FAILED"
        assert directory.exists() and not ProcessRunner.directory_stopped(directory)
        assert (storage.root / ".reservations" / f"{task.id}.json").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
    repository.finish(task.id, "failed", 0)
    from app.modules.media.repository import MediaRepository
    await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository)
    assert not directory.exists() and not (storage.root / ".reservations" / f"{task.id}.json").exists()


@pytest.mark.parametrize("marker,payload", [(".process.json", '{"pgid":'), ("nested/.process.json", '{"pgid":'),
    (".operations/invalid.json", '{"pgid":'), ("nested/.process.json", json.dumps({"pgid": 2**100})),
    ("nested/.process.json", json.dumps({"pgid": True}))])
def test_unknown_or_malformed_control_blocks_resource_release(storage, marker, payload):
    identifier = uuid4()
    with storage.reservation(identifier, 1024**2):
        directory = storage.create_task_directory(identifier)
        path = directory / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
        assert not ProcessRunner.directory_stopped(directory)
    assert (storage.root / ".reservations" / f"{identifier}.json").exists()


def test_spawn_unknown_and_bounded_legacy_scan_are_conservative(storage, monkeypatch):
    import app.infrastructure.process_control as controls
    directory = storage.create_task_directory(uuid4())
    FileProcessControl().prepare(directory)
    assert not ProcessRunner.directory_stopped(directory)
    for path in (directory / ".operations").glob("*.json"):
        path.unlink()
    (directory / "one" / "two" / "three").mkdir(parents=True)
    monkeypatch.setattr(controls, "MAX_DIRECTORIES", 2)
    assert not ProcessRunner.directory_stopped(directory)


def test_confirmed_exit_is_not_confused_with_recycled_pgid(storage, monkeypatch):
    directory = storage.create_task_directory(uuid4())
    registry = FileProcessControl()
    receipt = registry.prepare(directory)
    registry.started(receipt, 99999999, directory)
    registry.exited(receipt)
    monkeypatch.setattr(ProcessRunner, "group_exists", staticmethod(lambda _: True))
    assert ProcessRunner.directory_stopped(directory)


def test_unreadable_root_is_not_mistaken_for_absent_processes(storage, monkeypatch):
    directory = storage.create_task_directory(uuid4())
    original = Path.lstat
    def denied(path, *args, **kwargs):
        if path == directory:
            raise PermissionError("injected unreadable task directory")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", denied)
    assert not ProcessRunner.directory_stopped(directory)


async def test_qwen_preserves_temporary_audio_on_unconfirmed_process_exit(clips, monkeypatch):
    paths = []
    async def interrupted(self, command, directory, **controls):
        (directory / "chunk-00000.wav").write_bytes(b"still-being-written")
        paths.append(directory)
        raise AppException("PROCESS_CLEANUP_FAILED", "injected unconfirmed exit", 500)
    monkeypatch.setattr(ProcessRunner, "run", interrupted)
    provider = QwenASRProvider("https://example.com/v1", "test-no-network", "qwen3-asr-flash")
    with pytest.raises(AppException):
        await provider.transcribe(clips.service.files.path(clips.upload_id), "zh")
    assert (paths[0] / "chunk-00000.wav").exists()


def test_guarded_temporary_directory_cleans_normal_and_ordinary_failure(storage):
    paths = []
    with guarded_temporary_directory(prefix="normal-", dir=storage.root) as value:
        paths.append(Path(value))
    with pytest.raises(ValueError):
        with guarded_temporary_directory(prefix="failed-", dir=storage.root) as value:
            paths.append(Path(value))
            raise ValueError("ordinary error with no native reader")
    assert not any(path.exists() for path in paths)


async def timing_fixture(u, monkeypatch, entries, *, fail_process=False):
    plan = {"source_basis": "subtitle", "spec": {"strategy": {"min_seconds": 1, "max_seconds": 90}}}
    monkeypatch.setattr(u.clips.repository, "plan", lambda *args: plan)
    monkeypatch.setattr(u.repository, "perform_if_active", lambda task_id, writer: writer(None))
    monkeypatch.setattr(u.clips.repository, "store_timeline", lambda *args: None)
    task = SimpleNamespace(id=uuid4(), user_id=u.user.id, input_payload={"plan_id": str(uuid4()),
        "candidate_ids": [str(i) for i in range(len(entries))], "timing_model": "test", "timing_base_url": "https://example.com",
        "allowance": {"clip_limits": {"timing_timeout": 10, "max_timing_seconds": 900}}})
    directory = u.storage.create_task_directory(task.id)
    current, paths = [], []
    async def window(source, output, info, start, end, directory, **controls):
        output.write_bytes(b"wave")
        current[:] = [start, end]
        paths.append(output)
        if fail_process:
            raise AppException("PROCESS_CLEANUP_FAILED", "group exit unconfirmed", 500)
    async def transcribe(path, language):
        start, end = current
        return make_transcript("zh", [{"start": a - start, "end": b - start, "text": text}
            for a, b, text in entries if a >= start and b <= end])
    operation = prepare_timeline(task, u.clips, directory / "source.mp4",
        {"sha256": "a" * 64, "origin": 0, "audio_start": 0, "duration": 200}, directory,
        [{"id": str(i), "start": a, "end": b, "quote": text, "title": str(i)} for i, (a, b, text) in enumerate(entries)],
        SimpleNamespace(audio_window=window), SimpleNamespace(available=True, model="test", transcribe=transcribe),
        {"is_cancel_requested": AsyncMock(return_value=False)}, AsyncMock())
    return operation, paths


async def test_timing_audio_is_preserved_when_exit_not_confirmed(clips, monkeypatch):
    operation, paths = await timing_fixture(clips, monkeypatch, [(0, 30, TEXTS[0])], fail_process=True)
    with pytest.raises(AppException) as error:
        await operation
    assert error.value.code == "PROCESS_CLEANUP_FAILED" and paths[0].exists()


@pytest.mark.parametrize("entries", [[(0, 40, TEXTS[0]), (40, 80, TEXTS[1])], [(0, 90, TEXTS[0])]])
async def test_adjacent_and_exact_maximum_ranges_complete_alignment(clips, monkeypatch, entries):
    operation, paths = await timing_fixture(clips, monkeypatch, entries)
    timeline = await operation
    ordered = sorted(timeline["items"], key=lambda item: item["start"])
    assert all(item["end"] - item["start"] <= 90 + 1e-6 for item in ordered)
    assert all(a["end"] <= b["start"] for a, b in zip(ordered, ordered[1:]))
    assert not any(path.exists() for path in paths)
    assert timeline["version"] == "timing-v3"


@pytest.mark.parametrize("gap", [0, .001, .1, .25, 1])
def test_handles_fit_small_gaps_and_preserve_every_core(gap):
    items = [{"candidate_id": "b", "start": 40 + gap, "end": 80}, {"candidate_id": "a", "start": 0, "end": 40}]
    output = playback_ranges(items, {"min_seconds": 1, "max_seconds": 90}, 100)
    assert [item["candidate_id"] for item in output] == ["b", "a"]
    assert output[1]["end"] <= output[0]["start"]
    for original, padded in zip(items, output):
        assert padded["start"] <= original["start"] and padded["end"] >= original["end"]


def test_handles_shrink_instead_of_cutting_near_limit_core():
    item = {"candidate_id": "a", "start": 2, "end": 91.95}
    result = playback_ranges([item], {"min_seconds": 1, "max_seconds": 90}, 100)[0]
    assert abs(result["end"] - result["start"] - 90) < 1e-6
    assert result["core_start"] == 2 and result["core_end"] == 91.95


@pytest.mark.parametrize("entries", [[(0, 40), (39, 80)], [(0, 90.1)]])
def test_real_core_overlap_and_overlong_core_still_fail(entries):
    items = [{"candidate_id": str(i), "start": a, "end": b} for i, (a, b) in enumerate(entries)]
    with pytest.raises(AppException):
        playback_ranges(items, {"min_seconds": 1, "max_seconds": 90}, 200)
