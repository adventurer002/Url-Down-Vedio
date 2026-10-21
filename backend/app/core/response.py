from typing import Any

from fastapi.responses import JSONResponse


def success_response(data: Any) -> dict[str, Any]:
    return {"data": data}


def error_response(code: str, message: str, request_id: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={"X-Request-ID": request_id},
    )
