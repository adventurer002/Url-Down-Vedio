"""Progress fan-out: Redis pub/sub for live SSE + throttled DB writes for replay."""

import json
import time
import uuid
from collections.abc import Callable

import redis as sync_redis


def progress_channel(task_id: uuid.UUID) -> str:
    return f"task_progress:{task_id}"


def cancel_key(task_id: uuid.UUID) -> str:
    return f"task_cancel:{task_id}"


def event_payload(task_id: uuid.UUID, status: str, progress: float) -> str:
    return json.dumps({"task_id": str(task_id), "status": status, "progress": progress})


class ProgressReporter:
    """Worker-side: publish every hook, UPDATE db at most once per second."""

    def __init__(self, redis_client: sync_redis.Redis, task_id: uuid.UUID) -> None:
        self._redis = redis_client
        self._task_id = task_id
        self._last_db_write = 0.0

    def report(
        self,
        status: str,
        progress: float,
        db_write: Callable[[str, float], None] | None = None,
    ) -> None:
        self._redis.publish(progress_channel(self._task_id), event_payload(self._task_id, status, progress))
        now = time.monotonic()
        if db_write is not None and now - self._last_db_write >= 1.0:
            self._last_db_write = now
            db_write(status, progress)

    def final(self, status: str) -> None:
        self._redis.publish(
            progress_channel(self._task_id),
            json.dumps({"task_id": str(self._task_id), "status": status, "end": True}),
        )


def cancel_requested(redis_client: sync_redis.Redis, task_id: uuid.UUID) -> bool:
    return bool(redis_client.get(cancel_key(task_id)))
