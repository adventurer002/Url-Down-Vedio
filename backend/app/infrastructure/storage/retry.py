"""Bounded, cancellation-aware waits for storage admission; never replay task bodies."""
import asyncio
from contextlib import ExitStack, asynccontextmanager

from app.core.exceptions import AppException

LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.1


@asynccontextmanager
async def wait_for_storage(factory, *, is_cancel_requested=None, on_wait=None):
    with ExitStack() as guard:
        reported = False
        try:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                while True:
                    if is_cancel_requested is not None and await is_cancel_requested():
                        raise AppException("TASK_CANCELLED", "任务已取消", 409)
                    try:
                        guard.enter_context(factory())
                        break
                    except AppException as error:
                        # A duplicate reservation (409), quota failure or body failure is not transient admission.
                        if error.code != "STORAGE_BUSY" or error.status_code != 503:
                            raise
                    if not reported and on_wait is not None:
                        await on_wait()
                        reported = True
                    await asyncio.sleep(LOCK_POLL_SECONDS)
        except TimeoutError:
            raise AppException("STORAGE_BUSY", "存储持续繁忙，等待超时，请稍后重试", 503) from None
        yield
