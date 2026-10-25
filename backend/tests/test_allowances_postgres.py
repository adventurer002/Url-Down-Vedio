"""Real PostgreSQL row locks, concurrent task CAS and isolated-schema migration QA."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.infrastructure.database.base import Base
from app.modules.auth.repository import UserRepository
from app.modules.billing.models import Allowance
from app.modules.billing.policy import now
from app.modules.billing.repository import AllowanceRepository
from app.modules.tasks.repository import TaskRepository
from app.modules.video.repository import VideoRepository
from app.modules.video.parsers.base import ParsedVideo


@pytest.fixture
def postgres(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", True)
    engine = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1}, pool_pre_ping=True)
    schema = "test_allowances_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    except Exception:
        engine.dispose()
        pytest.skip("PostgreSQL unavailable; real lock tests require the configured local test-capable database")
    scoped = create_engine(settings.DATABASE_URL, pool_size=5,
        connect_args={"connect_timeout": 1, "options": f"-c search_path={schema} -c statement_timeout=5000"})
    try:
        Base.metadata.create_all(scoped)
        sessions = sessionmaker(bind=scoped, expire_on_commit=False, autoflush=False)
        tasks, quotas = TaskRepository(sessions), AllowanceRepository(sessions)
        user = UserRepository(sessions).subscription("pg-allowances-user", True, login=True)
        video = ParsedVideo("youtube", "pg-fixture", "fixture", duration=60, formats=[])
        video_id = VideoRepository(sessions).get_or_create(video, "https://example.com/watch")

        def create(action="ai_summary"):
            quote = quotas.save_quote(user.id, action, {"video_id": str(video_id), "duration_seconds": 60,
                "output_language": "zh", "content_version": settings.TRANSCRIPT_CONTENT_VERSION})
            _, payload, _ = quotas.quote(user.id, UUID(quote["quote_id"]))
            return tasks.create(action, video_id, payload, now() + timedelta(hours=24), user.id)

        yield sessions, tasks, quotas, user, create
    finally:
        scoped.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_postgres_duplicate_start_and_last_allowance(postgres):
    sessions, tasks, quotas, user, create = postgres
    quotas.balance(user.id)
    with sessions.begin() as session:
        for lot in session.scalars(select(Allowance).where(Allowance.kind == "ai_generations")):
            lot.granted = 1
    first, second = create(), create()
    barrier = Barrier(2)

    def start(task):
        barrier.wait(timeout=5)
        return tasks.start(task.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, (first, second)))
    assert sum(results) == 1
    winner = first if results[0] else second
    assert not tasks.start(winner.id)
    assert quotas.balance(user.id)["balances"]["ai_generations"] == {"available": 0, "held": 1}
    tasks.complete_with(winner.id, {"result_id": str(uuid4())}, lambda session: None)
    loser = second if results[0] else first
    assert not tasks.start(loser.id)
    assert tasks.get(loser.id).wait_reason == "allowance"


def test_postgres_completion_cancel_race_has_one_financial_outcome(postgres):
    _, tasks, quotas, user, create = postgres
    for _ in range(5):
        task = create()
        assert tasks.start(task.id)
        before = quotas.balance(user.id)["balances"]["ai_generations"]["available"]
        barrier = Barrier(2)

        def complete():
            barrier.wait(timeout=5)
            return tasks.complete_with(task.id, {"result_id": str(uuid4())}, lambda session: None)

        def cancel():
            barrier.wait(timeout=5)
            return tasks.request_cancel(task.id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(complete), pool.submit(cancel)]
            for future in futures:
                future.result(timeout=10)
        final = tasks.get(task.id)
        if final.cancel_requested and final.status != "completed":
            final = tasks.finish(task.id, "cancelled", 0)
        amounts = quotas.balance(user.id)["balances"]["ai_generations"]
        assert final.status in {"completed", "cancelled"}
        assert amounts["held"] == 0
        assert amounts["available"] == before + (final.status == "cancelled")


def test_postgres_concurrent_quote_confirmation_is_one_task(postgres):
    _, tasks, quotas, user, create = postgres
    task = create()
    quote_id = UUID(task.input_payload["quote_id"])
    _, payload, _ = quotas.quote(user.id, quote_id)
    with ThreadPoolExecutor(max_workers=3) as pool:
        identifiers = list(pool.map(lambda _: tasks.create("ai_summary", task.video_id, payload,
            now() + timedelta(hours=24), user.id).id, range(3)))
    assert identifiers == [task.id] * 3
