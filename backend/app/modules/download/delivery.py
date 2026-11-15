"""Prepare in a Worker, then serve an authenticated bounded HTTP transfer."""
import asyncio
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi.responses import RedirectResponse, StreamingResponse

from app.core.exceptions import AppException
from app.modules.auth.dependencies import check_owner
from app.modules.billing.policy import now, seconds
from app.modules.download.presets import select_streams, source_selection
from app.modules.download.relay import CHUNK_BYTES, TRANSFER_SECONDS, PublicRelayAdapter
from app.modules.download.repository import DeliveryRepository
from app.modules.media.service import safe_filename
from app.modules.tasks.service import database_call
from app.modules.video.parsers.cached import get_video_parser
from app.modules.video.parsers.runtime import server_proxy


class DeliveryPreparer:
    def __init__(self, parser=None, adapter=None):
        self.parser = parser or get_video_parser()
        self.adapter = adapter or PublicRelayAdapter()

    async def prepare(self, task, maximum):
        if (task.task_type != "download" or task.user_id is None or
                task.input_payload.get("delivery_preference") == "server" or server_proxy(task.input_payload["url"])):
            return None
        video = await self.parser.parse(task.input_payload["url"])
        if (video.platform, video.platform_video_id) != (task.input_payload["platform"], task.input_payload["platform_video_id"]):
            raise AppException("VIDEO_ID_CHANGED", "视频身份发生变化，请重新解析", 409)
        if seconds(video.duration) > task.input_payload.get("allowance", {}).get("limits", {}).get("video_seconds", 86400):
            raise AppException("VIDEO_TOO_LONG", "实际视频时长超过当前权益", 422)
        if task.input_payload["preset_id"] == "video_source_mp4" and source_selection(video) != task.input_payload.get("source_selection"):
            raise AppException("SOURCE_SELECTION_CHANGED", "源画幅或格式发生变化，请重新报价", 409)
        streams = select_streams(video, task.input_payload["preset_id"])
        stream = streams[0]
        if (len(streams) != 1 or stream.get("protocol", "https") not in {"http", "https"}
                or stream.get("ext") != "mp4" or stream.get("fragments")
                or stream.get("acodec") == "none"
                or stream.get("vcodec") == "none"):
            return None
        source = {"url": stream["url"], "headers": stream.get("http_headers", {}),
                  "duration_seconds": seconds(video.duration), "filename": safe_filename(video.title, "mp4")}
        return await self.adapter.inspect(source, maximum)


async def prepare_with_cancellation(preparer, task, maximum, cancelled):
    operation = asyncio.create_task(preparer.prepare(task, maximum))
    try:
        async with asyncio.timeout(55):
            while not operation.done():
                if await cancelled():
                    raise AppException("TASK_CANCELLED", "任务已取消", 409)
                await asyncio.wait({operation}, timeout=0.5)
            return await operation
    except TimeoutError:
        raise AppException("DOWNLOAD_TIMEOUT", "下载方式检查超时", 504) from None
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


class DeliveryService:
    def __init__(self, tasks, repository, billing=None, adapter=None):
        self.tasks, self.repository, self.billing = tasks, repository, billing
        self.adapter = adapter or PublicRelayAdapter()

    async def authorize(self, identifier, user, *, allow_expired=False):
        task = await database_call(self.tasks.repository.get, identifier)
        check_owner(task.user_id, user)
        if task.status != "completed" or (task.result_payload or {}).get("delivery_id") != str(identifier):
            raise AppException("DELIVERY_NOT_FOUND", "下载尚未准备完成", 404)
        delivery = await database_call(self.repository.get, identifier, user.id)
        if not allow_expired:
            from app.modules.billing.policy import aware
            if aware(delivery["expires_at"]) <= now():
                raise AppException("DELIVERY_EXPIRED", "下载来源已过期，请生成成品下载", 409)
        return task

    async def fallback(self, identifier, user):
        task = await self.authorize(identifier, user, allow_expired=True)
        previous = await database_call(self.repository.get, identifier, user.id)
        if previous["fallback_task_id"]:
            return await database_call(self.tasks.repository.get, previous["fallback_task_id"])
        payload = {key: value for key, value in task.input_payload.items()
                   if key not in {"quote_id", "allowance", "_execution_id"}}
        payload.update(delivery_preference="server", delivery_origin=str(identifier))
        proposal = await database_call(self.repository.billing.save_quote, user.id, "download", payload)
        quote_id = await database_call(self.repository.select_fallback_quote, identifier, user.id, UUID(proposal["quote_id"]))
        return await self.billing.confirm(user.id, quote_id)

    async def response(self, identifier, user, request):
        await self.authorize(identifier, user)
        range_header = request.headers.get("range")
        if request.headers.get("if-range"):
            details = await database_call(self.repository.get, identifier, user.id)
            if request.headers["if-range"] != details["etag"]:
                range_header = None
        lease = await database_call(self.repository.reserve, identifier, user.id, range_header)
        connection = None
        try:
            connection = await self.adapter.open(lease.source, lease.maximum, range_header)
            if lease.source.get("delivery_mode") == "direct":
                await connection.close()
                connection = None
                await database_call(self.repository.finish, identifier, user.id, lease, 0)
                return RedirectResponse(lease.source["url"], status_code=302,
                    headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
            headers = {"Content-Length": str(connection.length), "Cache-Control": "private, no-store",
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(lease.source["filename"], safe=""),
                "X-Content-Type-Options": "nosniff", "X-Accel-Buffering": "no",
                "Accept-Ranges": "bytes" if lease.source["range_supported"] else "none"}
            if lease.source["etag"]:
                headers["ETag"] = lease.source["etag"]
            if range_header:
                headers["Content-Range"] = connection.response.headers["content-range"]
            return MeteredRelayResponse(connection, self.repository, identifier, user.id, lease,
                status_code=206 if range_header else 200, headers=headers)
        except BaseException as error:
            if connection:
                await connection.close()
            await asyncio.shield(database_call(self.repository.finish, identifier, user.id, lease, 0,
                getattr(error, "code", "DELIVERY_UNAVAILABLE")))
            if isinstance(error, (httpx.HTTPError, TimeoutError)):
                raise AppException("DELIVERY_UNAVAILABLE", "下载来源连接失败，请重试或生成成品", 502) from None
            raise


class MeteredRelayResponse(StreamingResponse):
    def __init__(self, connection, repository, identifier, user_id, lease, **kwargs):
        self.connection, self.repository = connection, repository
        self.identifier, self.user_id, self.lease = identifier, user_id, lease
        self.received = 0
        super().__init__(self.stream_body(), media_type="video/mp4", **kwargs)

    async def stream_body(self):
        async for chunk in self.connection.response.aiter_raw(CHUNK_BYTES):
            self.received += len(chunk)
            if self.received > self.connection.length:
                raise AppException("DELIVERY_CHANGED", "来源传输大小发生变化", 502)
            yield chunk
        if self.received != self.connection.length:
            raise AppException("DELIVERY_INTERRUPTED", "来源传输中断，请重试或生成成品", 502)

    async def __call__(self, scope, receive, send):
        sent = 0
        error = "DELIVERY_INTERRUPTED"

        async def metered(message):
            nonlocal sent
            await send(message)
            if message["type"] == "http.response.body":
                sent += len(message.get("body", b""))

        try:
            async with asyncio.timeout(TRANSFER_SECONDS):
                await super().__call__(scope, receive, metered)
            if sent == self.connection.length:
                error = None
        finally:
            try:
                await self.connection.close()
            finally:
                await asyncio.shield(database_call(self.repository.finish, self.identifier, self.user_id,
                    self.lease, sent, error))


def delivery_service(sessions, tasks=None, billing=None):
    from app.modules.billing.router import get_billing_service
    from app.modules.tasks.repository import TaskRepository
    from app.modules.tasks.service import TaskService
    billing = billing or get_billing_service()
    tasks = tasks or TaskService(TaskRepository(sessions), billing.tasks.bus)
    return DeliveryService(tasks, DeliveryRepository(sessions), billing)
