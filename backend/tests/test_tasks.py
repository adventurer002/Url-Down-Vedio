import asyncio
import json
import os
import shutil
import subprocess
import time
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import AppException
from app.infrastructure.database.base import Base
from app.infrastructure.queue.progress_bus import ProgressBus
from app.main import create_app
from app.modules.tasks.models import Task, TaskStatus
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.tasks.router import get_task_service
from app.modules.tasks.service import TaskService
from app.modules.video.models import Video


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    repo = TaskRepository(sessionmaker(bind=engine, expire_on_commit=False))
    yield repo
    engine.dispose()


class MemoryBus:
    """Service orchestration double; the actual Lua script is tested on Redis below."""
    def __init__(self):
        self.value = None
        self.calls = []
        self.flag = False
        self.subscription = Mock(receive=AsyncMock(), close=AsyncMock())

    async def publish(self, task_id, status, progress, stage=None, error_code=None):
        self.calls.append("publish")
        self.value = {"task_id": str(task_id), "status": status, "progress": progress,
                      "stage": stage or status, "error_code": error_code, "seq": (self.value or {}).get("seq", 0) + 1,
                      "updated_at": utcnow().isoformat()}
        return self.value

    async def snapshot(self, task_id):
        self.calls.append("snapshot")
        return self.value

    async def subscribe(self, task_id):
        self.calls.append("subscribe_ack")
        return self.subscription

    async def request_task_cancel(self, task_id, persist):
        self.flag = await persist()
        return self.flag

    async def is_cancel_requested(self, task_id, persisted):
        return self.flag or await persisted()


async def test_creation_goes_through_service_and_dispatcher(repository):
    dispatcher = Mock()
    bus = MemoryBus()
    service = TaskService(repository, bus, dispatcher)
    task = await service.create(input_payload={"demo": True})
    stored = repository.get(task.id)
    assert stored.status == "queued" and stored.progress == 0 and stored.attempts == 0
    assert stored.input_payload == {"demo": True} and stored.expires_at is not None
    assert bus.value["status"] == "queued"
    dispatcher.enqueue.assert_called_once_with(task.id)


async def test_dispatch_failure_becomes_durable_failed_state(repository):
    dispatcher = Mock(enqueue=Mock(side_effect=RuntimeError("secret broker URL")))
    bus = MemoryBus()
    with pytest.raises(AppException) as exc:
        await TaskService(repository, bus, dispatcher).create()
    assert exc.value.code == "TASK_DISPATCH_FAILED" and "secret" not in exc.value.message
    with repository.sessions() as session:
        task = session.scalar(select(Task))
        assert task.status == TaskStatus.failed and task.error_code == "TASK_DISPATCH_FAILED"


@pytest.mark.parametrize("terminal", ["completed", "failed", "expired", "cancelled"])
async def test_terminal_cancel_cannot_overwrite_cas(repository, terminal):
    task = repository.create("simulate_progress")
    assert repository.start(task.id)
    before = repository.get(task.id)  # stale read: worker finishes after client saw running
    finished = repository.finish(task.id, terminal, 100 if terminal == "completed" else 50,
                                  result_payload={"done": True} if terminal == "completed" else None)
    assert before.status == "running"
    bus = MemoryBus()
    response = await TaskService(repository, bus).cancel(task.id)
    current = repository.get(task.id)
    assert current.status == terminal and current.finished_at == finished.finished_at
    assert current.result_payload == finished.result_payload
    assert response["status"] == terminal
    assert not current.cancel_requested and not bus.flag


def test_cancellation_wins_completion_race(repository):
    task = repository.create("simulate_progress")
    assert repository.start(task.id)
    repository.request_cancel(task.id)
    finished = repository.finish(task.id, "completed", 100, result_payload={"should": "not persist"})
    assert finished.status == "cancelled" and finished.result_payload is None


@pytest.mark.parametrize("status", ["queued", "running", "processing"])
async def test_queued_or_cooperative_cancel(repository, status):
    task = repository.create("simulate_progress")
    if status != "queued":
        repository.start(task.id)
    if status == "processing":
        repository.mark_processing(task.id)
    bus = MemoryBus()
    result = await TaskService(repository, bus).cancel(task.id)
    assert result["cancel_requested"] and bus.flag
    assert result["status"] == ("cancelled" if status == "queued" else status)
    if status == "queued":
        assert not repository.start(task.id)


def test_duplicate_delivery_cannot_start_twice(repository):
    task = repository.create("simulate_progress")
    assert repository.start(task.id)
    assert not repository.start(task.id)
    assert repository.get(task.id).attempts == 1


def test_database_constraints_and_video_identity(repository):
    values = dict(platform="youtube", platform_video_id="abc", source_url="https://example.com", title="sample")
    with repository.sessions.begin() as session:
        session.add(Video(**values))
    with pytest.raises(IntegrityError), repository.sessions.begin() as session:
        session.add(Video(**values))
    task = repository.create("simulate_progress")
    with pytest.raises(IntegrityError), repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == task.id).values(progress=101))
    with pytest.raises(IntegrityError):
        repository.create("simulate_progress", video_id=uuid4())


async def test_sse_subscribe_ack_before_any_snapshot_and_dedup(repository):
    task = repository.create("simulate_progress")
    bus = MemoryBus()
    service = TaskService(repository, bus)
    subscription, first = await service.open_events(task.id)
    assert bus.calls[0] == "subscribe_ack"
    assert bus.calls.index("subscribe_ack") < bus.calls.index("snapshot")
    assert first["seq"] == 1 and first["status"] == "queued"
    subscription.receive.side_effect = [first, {**first, "seq": 2, "status": "completed", "progress": 100}]
    events = [item async for item in service.events(task.id, subscription, first)]
    assert [item["seq"] for item in events] == [1, 2]
    subscription.close.assert_awaited_once()


async def test_subscribe_snapshot_race_uses_newest_snapshot(repository):
    task = repository.create("simulate_progress")
    repository.start(task.id)
    bus = MemoryBus()
    service = TaskService(repository, bus)
    original = bus.subscribe

    async def subscribe(task_id):
        subscription = await original(task_id)
        # Progress was published between subscription and reading the snapshot.
        await bus.publish(task_id, "running", 40)
        await bus.publish(task_id, "running", 60)
        return subscription

    bus.subscribe = subscribe
    subscription, first = await service.open_events(task.id)
    assert first["progress"] == 60 and first["seq"] == 2
    subscription.receive.side_effect = [{**first, "seq": 1}, {**first, "seq": 3, "status": "cancelled"}]
    values = [item async for item in service.events(task.id, subscription, first)]
    assert [value["seq"] for value in values] == [2, 3]


async def test_terminal_sse_sends_first_frame_and_closes(repository):
    task = repository.create("simulate_progress")
    repository.finish(task.id, "completed", 100)
    service = TaskService(repository, MemoryBus())
    sub, initial = await service.open_events(task.id)
    assert [item async for item in service.events(task.id, sub, initial)] == [initial]
    sub.receive.assert_not_awaited()
    sub.close.assert_awaited_once()


async def test_not_found_sse_closes_subscription(repository):
    bus = MemoryBus()
    with pytest.raises(AppException) as error:
        await TaskService(repository, bus).open_events(uuid4())
    assert error.value.code == "TASK_NOT_FOUND"
    bus.subscription.close.assert_awaited_once()


async def test_api_task_and_terminal_stream(repository):
    task = repository.create("simulate_progress")
    repository.finish(task.id, "completed", 100, result_payload={"simulated": True})
    service = TaskService(repository, MemoryBus())
    app = create_app()
    app.dependency_overrides[get_task_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/v1/tasks/{task.id}")
        assert detail.status_code == 200 and detail.json()["data"]["status"] == "completed"
        cancelled = await client.post(f"/api/v1/tasks/{task.id}/cancel")
        assert cancelled.json()["data"]["status"] == "completed"
        response = await client.get(f"/api/v1/tasks/{task.id}/events")
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        assert "event: progress" in response.text and '"status": "completed"' in response.text
        missing = await client.get(f"/api/v1/tasks/{uuid4()}")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "TASK_NOT_FOUND"


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory):
    if os.environ.get("TEST_REDIS_URL"):
        yield os.environ["TEST_REDIS_URL"]
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("Redis integration tests require redis-server or TEST_REDIS_URL")
    folder = tmp_path_factory.mktemp("redis")
    # Unix socket needs no network port and is isolated from development Redis.
    path = folder / "redis.sock"
    with open(folder / "redis.log", "w") as log:
        process = subprocess.Popen([binary, "--port", "0", "--unixsocket", str(path), "--unixsocketperm", "700",
                                    "--save", "", "--appendonly", "no", "--dir", str(folder)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while not path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not path.exists():
                pytest.fail("isolated redis-server could not start: " + (folder / "redis.log").read_text())
            yield f"unix://{path}?db=0"
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


@pytest.fixture
async def real_bus(redis_url):
    redis = Redis.from_url(redis_url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1,
                           retry=Retry(NoBackoff(), 0))
    yield ProgressBus(redis, ttl=10)
    await redis.aclose()


async def test_progress_throttle_seq_snapshot_and_atomic_publish(real_bus):
    task_id = uuid4()
    sub = await real_bus.subscribe(task_id)
    try:
        queued = await real_bus.publish(task_id, "queued", 0)
        running = await real_bus.publish(task_id, "running", 1)  # state changes bypass throttle
        assert running["seq"] == queued["seq"] + 1
        assert await real_bus.publish(task_id, "running", 20) is None
        await asyncio.sleep(0.52)
        progress = await real_bus.publish(task_id, "running", 40)
        assert progress["seq"] == running["seq"] + 1 and progress["progress"] == 40
        final = await real_bus.publish(task_id, "completed", 100)
        assert final["seq"] == progress["seq"] + 1
        assert await real_bus.snapshot(task_id) == final
        assert await real_bus.redis.ttl(f"task:snapshot:{task_id}") > 0
        assert await real_bus.publish(task_id, "running", 5) is None
        assert await real_bus.publish(task_id, "cancelled", 50) is None
        received = []
        async with asyncio.timeout(3):
            while len(received) < 4:
                item = await sub.receive()
                if item:
                    received.append(item)
        assert received == [queued, running, progress, final]
    finally:
        await sub.close()


async def test_sequence_survives_snapshot_expiry_and_publishers_are_serialized(real_bus):
    task_id = uuid4()
    initial = await real_bus.publish(task_id, "queued", 0)
    await real_bus.redis.delete(f"task:snapshot:{task_id}")
    results = await asyncio.gather(*(real_bus.publish(task_id, "running", number) for number in range(10)))
    published = [result for result in results if result]
    assert len(published) == 1 and published[0]["seq"] == initial["seq"] + 1


async def test_cancel_flag_is_written_only_after_successful_pg_cas(real_bus, repository):
    service = TaskService(repository, real_bus)
    queued = repository.create("simulate_progress")
    await service.cancel(queued.id)
    assert await real_bus.redis.exists(f"task:cancel:{queued.id}")
    assert repository.get(queued.id).cancel_requested
    complete = repository.create("simulate_progress")
    repository.finish(complete.id, "completed", 100)
    await service.cancel(complete.id)
    assert not await real_bus.redis.exists(f"task:cancel:{complete.id}")
    await real_bus.redis.delete(f"task:cancel:{queued.id}")
    assert await service.is_cancel_requested(queued.id)  # durable fallback after TTL
