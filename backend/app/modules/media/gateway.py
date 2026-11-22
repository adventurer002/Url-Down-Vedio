"""Separate ASGI service for private object reads and actual-byte metering."""
import asyncio
import logging
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response
import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.logging import install_log_redaction
from app.infrastructure.storage.objects import object_key, object_store, trusted_origin
from app.modules.billing.transfer import transfer_range
from app.modules.media.delivery import decode_ticket, delivery_secret

logger = logging.getLogger(__name__)


class TrafficCallbacks:
    async def report(self, identifier, action, amount):
        base = trusted_origin(settings.OBJECT_GATEWAY_API_URL)
        try:
            async with asyncio.timeout(4), httpx.AsyncClient(
                timeout=httpx.Timeout(3, connect=1), follow_redirects=False, trust_env=False,
            ) as client:
                reply = await client.post(f"{base}/api/v1/internal/object-delivery/{identifier}/{action}",
                    json={"bytes": amount}, headers={"X-Object-Gateway-Key": delivery_secret()})
        except (httpx.HTTPError, TimeoutError):
            raise AppException("OBJECT_METER_UNAVAILABLE", "下载计量服务暂不可用", 503) from None
        if reply.status_code == 409 and action == "claim":
            raise AppException("OBJECT_TICKET_USED", "对象下载凭证已使用或过期", 409)
        if reply.status_code != 200:
            raise AppException("OBJECT_METER_UNAVAILABLE", "下载计量服务暂不可用", 503)


def error_response(error):
    request_id = "req_" + uuid4().hex
    return JSONResponse({"error": {"code": error.code, "message": error.message, "request_id": request_id}},
        status_code=error.status_code, headers={"Cache-Control": "private, no-store", "X-Request-ID": request_id})


class MeteredObjectResponse(Response):
    def __init__(self, delivery, store, callbacks):
        super().__init__()
        self.delivery, self.store, self.callbacks = delivery, store, callbacks

    async def __call__(self, scope, receive, send):
        delivery = self.delivery
        sent = 0
        started = claimed = False

        async def meter(message):
            nonlocal sent, started
            if message["type"] == "http.response.start":
                started = True
            if message["type"] == "http.response.body":
                size = len(message.get("body", b""))
                if sent + size > delivery.length:
                    raise AppException("OBJECT_READ_FAILED", "对象字节数与下载范围不符", 502)
                await send(message)
                sent += size
            else:
                await send(message)

        try:
            async with asyncio.timeout(600):
                await self.callbacks.report(delivery.transfer_id, "claim", delivery.length)
                claimed = True
                async with self.store.read(object_key(delivery.tag, delivery.storage_key), delivery.byte_range) as upstream:
                    content_range = None
                    if delivery.bounds:
                        content_range = f"bytes {delivery.bounds[0]}-{delivery.bounds[1]}/{delivery.size}"
                    status = 206 if content_range else 200
                    if (upstream.status_code != status or upstream.headers.get("content-length") != str(delivery.length)
                            or upstream.headers.get("content-encoding", "identity") != "identity"
                            or upstream.headers.get("content-range") != content_range):
                        raise AppException("OBJECT_READ_FAILED", "对象响应与已授权下载范围不符", 502)
                    headers = {"Content-Length": str(delivery.length), "Accept-Ranges": "bytes",
                               "Content-Disposition": f"attachment; filename*=UTF-8''{quote(delivery.filename, safe='')}",
                               "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
                    if content_range:
                        headers["Content-Range"] = content_range
                    response = StreamingResponse(upstream.aiter_raw(), status_code=status,
                                                 media_type=delivery.mime_type, headers=headers)
                    await response(scope, receive, meter)
        except (AppException, TimeoutError) as cause:
            error = cause if isinstance(cause, AppException) else AppException("OBJECT_READ_TIMEOUT", "对象下载超时", 504)
            if not started:
                await error_response(error)(scope, receive, send)
            else:
                raise RuntimeError("Object delivery interrupted") from None
        finally:
            if claimed:
                async def settle():
                    for _ in range(2):
                        try:
                            await self.callbacks.report(delivery.transfer_id, "settle", sent)
                            return
                        except AppException:
                            pass
                    logger.warning("对象下载结算等待对账 transfer_id=%s", delivery.transfer_id)
                await asyncio.shield(settle())


def create_gateway(*, store_factory=object_store, callbacks=None):
    install_log_redaction()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    callbacks = callbacks or TrafficCallbacks()

    @app.get("/download")
    async def download(request: Request, token: str = Query(max_length=4096)):
        try:
            delivery = decode_ticket(token)
            if transfer_range(delivery.size, request.headers.get("range")) != delivery.bounds:
                raise AppException("OBJECT_RANGE_MISMATCH", "下载范围与签发凭证不一致", 416)
            store = store_factory()
            if store.tag != delivery.tag:
                raise AppException("OBJECT_CONFIG_INVALID", "对象存储命名空间不一致", 503)
        except AppException as error:
            return error_response(error)
        return MeteredObjectResponse(delivery, store, callbacks)

    return app


app = create_gateway()
