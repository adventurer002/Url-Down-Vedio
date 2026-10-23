import asyncio

from fastapi import APIRouter

from app.core.exceptions import AppException
from app.core.response import success_response
from app.infrastructure.database.session import check_db_ready
from app.infrastructure.queue.redis_client import check_redis_ready
from app.modules.video.router import router as video_router
from app.modules.tasks.router import router as tasks_router
from app.modules.download.router import router as download_router
from app.modules.media.router import router as audio_router
from app.modules.transcript.router import router as transcript_router

from app.modules.ai.router import router as ai_router

from app.modules.auth.router import router as auth_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(ai_router)
from app.modules.system.router import router as system_router
router.include_router(system_router)
router.include_router(video_router)
router.include_router(tasks_router)
router.include_router(download_router)
router.include_router(audio_router)
router.include_router(transcript_router)


@router.get("/health/live", tags=["health"])
async def live():
    return success_response({"status": "alive"})


@router.get("/health/ready", tags=["health"])
async def ready():
    db_ready, redis_ready = await asyncio.gather(check_db_ready(), check_redis_ready())
    if not (db_ready and redis_ready):
        raise AppException(
            "DEPENDENCY_UNAVAILABLE",
            f"PostgreSQL: {'ready' if db_ready else 'unavailable'}; "
            f"Redis: {'ready' if redis_ready else 'unavailable'}",
            status_code=503,
        )
    return success_response({"status": "ready", "database": "ready", "redis": "ready"})
