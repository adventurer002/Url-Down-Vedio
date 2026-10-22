import asyncio

from fastapi import APIRouter, Depends

from app.core.exceptions import AppException
from app.core.response import success_response
from app.infrastructure.storage.cleanup import cleanup_expired_files

router = APIRouter(prefix="/system", tags=["system"])


class CleanupService:
    def run(self):
        # FastAPI executes this synchronous route in its thread pool, not the HTTP event loop.
        try:
            return asyncio.run(cleanup_expired_files())
        except OSError:
            raise AppException("STORAGE_CLEANUP_FAILED", "文件清理失败，请稍后重试", 503) from None


def get_cleanup_service():
    return CleanupService()


@router.post("/cleanup")
def cleanup(service=Depends(get_cleanup_service)):
    return success_response(service.run())
