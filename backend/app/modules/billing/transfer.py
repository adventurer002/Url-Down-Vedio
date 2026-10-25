import asyncio
import re

from fastapi.responses import FileResponse

from app.core.exceptions import AppException
from app.modules.tasks.service import database_call


def transfer_length(length, range_header=None, if_range=None):
    if not range_header:
        return length
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or not any(match.groups()):
        raise AppException("INVALID_RANGE", "首版仅支持单个 bytes Range", 416)
    start, end = match.groups()
    if start:
        start = int(start)
        end = min(int(end), length - 1) if end else length - 1
        if start >= length or end < start:
            raise AppException("INVALID_RANGE", "请求的文件范围无效", 416)
        amount = end - start + 1
    else:
        amount = min(int(end), length)
        if not amount:
            raise AppException("INVALID_RANGE", "请求的文件范围无效", 416)
    # A failed If-Range comparison makes Starlette send the whole file.
    return length if if_range else amount


class AllowanceFileResponse(FileResponse):
    def __init__(self, *args, repository, transfer_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.repository, self.transfer_id = repository, transfer_id

    async def __call__(self, scope, receive, send):
        sent = 0

        async def metered(message):
            nonlocal sent
            await send(message)
            if message["type"] == "http.response.body":
                sent += len(message.get("body", b""))

        scope = {**scope, "extensions": {key: value for key, value in scope.get("extensions", {}).items()
                                         if key != "http.response.pathsend"}}
        try:
            async with asyncio.timeout(600):
                await super().__call__(scope, receive, metered)
        finally:
            await asyncio.shield(database_call(self.repository.finish_transfer, self.transfer_id, sent))
