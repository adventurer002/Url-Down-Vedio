"""Stage wall time only: never log source URLs, prompts, transcripts or credentials."""
from contextlib import contextmanager
from datetime import timezone
import logging
import time

logger = logging.getLogger(__name__)


def log_queue_wait(task):
    if task.started_at and task.created_at:
        def aware(value):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        elapsed = max(0, (aware(task.started_at) - aware(task.created_at)).total_seconds())
        logger.info("performance stage=queue task_id=%s duration_ms=%d outcome=started", task.id, round(elapsed * 1000))


@contextmanager
def stage_timer(stage: str, task_id=None):
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "interrupted"
        raise
    finally:
        logger.info("performance stage=%s task_id=%s duration_ms=%d outcome=%s",
                    stage, task_id or "-", round((time.monotonic() - started) * 1000), outcome)
