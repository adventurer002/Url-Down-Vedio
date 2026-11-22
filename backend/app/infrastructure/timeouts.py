"""Inherited operation deadlines; retries share the same remaining budget."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.timing import logger


@dataclass(frozen=True)
class Deadline:
    expires_at: float
    task_id: str | None = None

    @property
    def remaining(self) -> float:
        return max(0, self.expires_at - asyncio.get_running_loop().time())

    def http_timeout(self) -> httpx.Timeout:
        remaining = self.remaining
        return httpx.Timeout(
            min(settings.CLOUD_READ_TIMEOUT_SECONDS, remaining),
            connect=min(settings.CLOUD_CONNECT_TIMEOUT_SECONDS, remaining),
            write=min(settings.CLOUD_WRITE_TIMEOUT_SECONDS, remaining),
            pool=min(5, remaining),
        )


_deadline: ContextVar[Deadline | None] = ContextVar("operation_deadline", default=None)


@asynccontextmanager
async def operation_timeout(stage: str, seconds: float, code: str, message: str, task_id=None):
    """Only the scope owning the earliest deadline converts its timeout error.

    A child inherits the parent's deadline, including in asyncio child tasks.
    External cancellation always propagates and cleanup is awaited by callers.
    """
    started = asyncio.get_running_loop().time()
    parent = _deadline.get()
    expires_at = started + seconds
    owns_timeout = parent is None or expires_at < parent.expires_at
    budget = Deadline(expires_at if owns_timeout else parent.expires_at,
                      str(task_id) if task_id is not None else parent.task_id if parent else None)
    token = _deadline.set(budget)
    timer = asyncio.timeout_at(budget.expires_at if owns_timeout else None)
    outcome, error_code = "ok", "-"
    try:
        async with timer:
            yield budget
    except TimeoutError:
        outcome = "timeout" if timer.expired() else "error"
        if timer.expired():
            error_code = code
            raise AppException(code, message, 504) from None
        raise
    except BaseException as error:
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
        error_code = error.code if isinstance(error, AppException) else "-"
        raise
    finally:
        _deadline.reset(token)
        logger.info(
            "performance stage=%s task_id=%s duration_ms=%d budget_ms=%d outcome=%s error_code=%s",
            stage, budget.task_id or "-", round((asyncio.get_running_loop().time() - started) * 1000),
            round(max(0, budget.expires_at - started) * 1000), outcome, error_code,
        )
