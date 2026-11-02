"""Real PostgreSQL serialization and clip-specific financial outcome checks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from test_allowances_postgres import postgres
from app.core.config import settings
from app.core.exceptions import AppException


def test_postgres_same_export_confirmations_conflict_before_duplicate_work(postgres, monkeypatch):
    _, tasks, quotas, user, create = postgres
    monkeypatch.setattr(settings, "CLIPS_ENABLED", True)
    seed = create("transcription")
    spec = {"video_id": str(seed.video_id), "duration_seconds": 60, "plan_id": str(uuid4()), "export_key": "a" * 64}
    payloads = []
    for _ in range(2):
        quote = quotas.save_quote(user.id, "clips_export", spec)
        payloads.append(quotas.quote(user.id, UUID(quote["quote_id"]))[1])
    barrier = Barrier(2)
    def confirm(payload):
        barrier.wait(timeout=5)
        try:
            return tasks.create("clips_export", seed.video_id, payload, user_id=user.id).id
        except AppException as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(confirm, payloads))
    assert values.count("CLIP_EXPORT_IN_PROGRESS") == 1
    assert sum(isinstance(value, UUID) for value in values) == 1


def test_postgres_owned_transcript_does_not_make_new_plan_free_and_empty_plan_releases(postgres, monkeypatch):
    _, tasks, quotas, user, create = postgres
    monkeypatch.setattr(settings, "CLIPS_ENABLED", True)
    old = create("transcription")
    transcript_id = str(uuid4())
    assert tasks.start(old.id)
    tasks.complete_with(old.id, {"transcript_id": transcript_id}, lambda session: None)
    before = quotas.balance(user.id)["balances"]["ai_generations"]["available"]
    for empty in (False, True):
        quote = quotas.save_quote(user.id, "ai_clip_plan", {"video_id": str(old.video_id), "source_kind": "transcript",
            "duration_seconds": 0, "transcript_id": transcript_id})
        payload = quotas.quote(user.id, UUID(quote["quote_id"]))[1]
        task = tasks.create("ai_clip_plan", old.video_id, payload, user_id=user.id)
        assert tasks.start(task.id)
        tasks.complete_with(task.id, {"clip_plan_id": str(uuid4()), "transcript_id": transcript_id,
            "outcome": "no_highlights" if empty else "highlights"}, lambda session: None)
    balance = quotas.balance(user.id)["balances"]["ai_generations"]
    assert balance == {"available": before - 1, "held": 0}


def test_postgres_runtime_cache_mode_preserves_serialized_render_limit(postgres, monkeypatch):
    _, tasks, quotas, user, create = postgres
    monkeypatch.setattr(settings, "CLIPS_ENABLED", True)
    monkeypatch.setattr(settings, "CLIPS_MAX_DAILY_EXPORTS", 1)
    seed = create("transcription")
    plan_id = uuid4()
    records = []
    for index in range(2):
        quote = quotas.save_quote(user.id, "clips_export", {"video_id": str(seed.video_id),
            "duration_seconds": 60, "plan_id": str(plan_id), "export_key": str(index) * 64})
        payload = quotas.quote(user.id, UUID(quote["quote_id"]))[1]
        records.append(tasks.create("clips_export", seed.video_id, payload, user_id=user.id))
    assert tasks.start(records[0].id)
    tasks.clip_execution_mode(records[0].id, "render")
    with pytest.raises(ValueError):
        tasks.clip_execution_mode(records[0].id, "cache")
    tasks.finish(records[0].id, "failed", 0)
    assert tasks.start(records[1].id, clip_cache_ready=True)
    tasks.clip_execution_mode(records[1].id, "cache")
    with pytest.raises(AppException):
        tasks.clip_execution_mode(records[1].id, "render")
    tasks.complete_with(records[1].id, {"clip_export_id": str(uuid4())}, lambda session: None)
    with tasks.sessions() as session:
        assert tasks.clip_render_counts(session, user.id, plan_id) == (1, 1)
