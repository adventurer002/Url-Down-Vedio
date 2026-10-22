from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
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
        return error_response(exc.code, exc.message, request.state.request_id, exc.status_code)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return error_response(code, str(exc.detail), request.state.request_id, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return error_response("VALIDATION_ERROR", "请求参数校验失败", request.state.request_id, 422)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return error_response("INTERNAL_SERVER_ERROR", "服务器内部错误", request.state.request_id, 500)

    app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS,
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"],
                       expose_headers=["X-Request-ID"], allow_credentials=False)
    app.include_router(router)
    return app


app = create_app()
