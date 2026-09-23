import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.ai import router as ai_router
from backend.app.api.auth import router as auth_router
from backend.app.api.billing import router as billing_router
from backend.app.api.health import router as health_router
from backend.app.api.media import router as media_router
from backend.app.core.config import settings
from backend.app.core.errors import AppError, app_error_handler
from backend.app.core.logging import configure_logging
from backend.app.core.middleware import RequestIdMiddleware
from backend.app.core.rate_limit import RateLimitMiddleware

configure_logging()

if settings.sentry_dsn:
    sentry_sdk.init(dsn=str(settings.sentry_dsn))

app = FastAPI(title="down-vedio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After"],
)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(RateLimitMiddleware)
app.include_router(auth_router)
app.include_router(ai_router)
app.include_router(billing_router)
app.include_router(health_router)
app.include_router(media_router)


@app.get("/debug-error")
async def debug_error() -> None:
    raise RuntimeError("intentional debug error for sentry verification")


@app.exception_handler(AppError)
async def app_error_route_handler(request: Request, exc: AppError) -> JSONResponse:
    return await app_error_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, RuntimeError) and str(exc).startswith("intentional"):
        raise exc
    if isinstance(exc, AppError):
        return await app_error_handler(request, exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Internal server error"}},
    )
