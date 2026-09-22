"""Unified error envelope per docs/api.md. Frontend branches on code only."""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

CODE_STATUS: dict[str, int] = {
    "invalid_url": 400,
    "url_not_allowed": 400,
    "unsupported_platform": 400,
    "validation_error": 422,
    "unauthorized": 401,
    "permission_denied": 403,
    "quota_exceeded": 429,
    "rate_limited": 429,
    "not_found": 404,
    "task_not_cancellable": 409,
    "prerequisite_missing": 409,
    "parse_failed": 502,
    "download_failed": 502,
    "internal_error": 500,
}


class AppError(Exception):
    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=CODE_STATUS.get(code, 500),
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.code, exc.message, exc.details)


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, AppError):
        return await app_error_handler(request, exc)
    return error_response("internal_error", "Internal server error")
