from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from app.core.config import settings, PROJECT_ROOT
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.api.v1.router import router
from app.core.exceptions import AppException
from app.core.response import error_response
from app.infrastructure.database.session import engine, readiness_engine
from app.infrastructure.queue.redis_client import redis_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()
    await readiness_engine.dispose()
    engine.dispose()


def create_app() -> FastAPI:
    from app.core.logging import install_log_redaction

    install_log_redaction()
    app = FastAPI(title="VideoHub AI", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        return error_response(
            exc.code, exc.message, request.state.request_id, exc.status_code
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
            exc.status_code, "HTTP_ERROR"
        )
        return error_response(
            code, str(exc.detail), request.state.request_id, exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        return error_response(
            "VALIDATION_ERROR", "请求参数校验失败", request.state.request_id, 422
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return error_response(
            "INTERNAL_SERVER_ERROR", "服务器内部错误", request.state.request_id, 500
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        expose_headers=["X-Request-ID"],
        allow_credentials=True,
    )
    app.include_router(router)

    # Serve the WeChat QR image so it loads whether the workspace is reached
    # via Vite (5173) or the backend host (8000 / public tunnel). A dedicated
    # route avoids a catch-all StaticFiles mount that would shadow test routes.
    qr_path = PROJECT_ROOT / "frontend" / "public" / "wechat_qr.png"

    @app.get("/wechat_qr.png", include_in_schema=False)
    async def serve_wechat_qr():
        if qr_path.is_file():
            return FileResponse(str(qr_path), media_type="image/png")
        raise HTTPException(status_code=404)

    return app


app = create_app()
