"""Stage wall time only: never log source URLs, prompts, transcripts or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
import logging
import os
import time

logger = logging.getLogger(__name__)
_timed_task = ContextVar("timed_task", default=None)
CHILD_STAGES = {"source_extract", "source_cached", "source_transfer"}


def log_child_stage(event):
    task_id = _timed_task.get()
    stage, started, duration = event.get("stage"), event.get("started_unix_ms"), event.get("duration_ms")
    if (task_id and isinstance(stage, str) and stage in CHILD_STAGES
            and type(started) is int and started > 0 and type(duration) is int and duration >= 0):
        logger.info("performance stage=%s task_id=%s duration_ms=%d outcome=ok started_unix_ms=%d pid=%d",
                    stage, task_id, duration, started, os.getpid())


def log_queue_wait(task):
    if task.started_at and task.created_at:
        def aware(value):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        elapsed = max(0, (aware(task.started_at) - aware(task.created_at)).total_seconds())
        logger.info("performance stage=queue task_id=%s duration_ms=%d outcome=started", task.id, round(elapsed * 1000))


@contextmanager
def stage_timer(stage: str, task_id=None):
    task_id = task_id or _timed_task.get()
    token = _timed_task.set(task_id)
    started_unix_ms = time.time_ns() // 1_000_000
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "interrupted"
        raise
    finally:
        _timed_task.reset(token)
        logger.info("performance stage=%s task_id=%s duration_ms=%d outcome=%s started_unix_ms=%d pid=%d",
                    stage, task_id or "-", round((time.monotonic() - started) * 1000), outcome,
                    started_unix_ms, os.getpid())
