"""Bounded public-HTTP adapter for single-file, byte-preserving delivery."""
import asyncio
from dataclasses import dataclass
import re
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.exceptions import AppException
from app.core.config import settings
from app.core.logging import sensitive_http_urls
from app.core.security import validate_url
from app.infrastructure.http_client import public_transport

CHUNK_BYTES = 64 * 1024
TRANSFER_SECONDS = 1200
PROBE_BYTES = 1024 * 1024


def mp4_codecs(data):
    """Read complete ISO BMFF metadata boxes, without decoding media or scanning payload strings."""
    def boxes(start, end):
        while start + 8 <= end:
            size = int.from_bytes(data[start:start + 4], "big")
            kind = bytes(data[start + 4:start + 8])
            header = 8
            if size == 1:
                if start + 16 > end:
                    return
                size, header = int.from_bytes(data[start + 8:start + 16], "big"), 16
            if size < header or start + size > end:
                return
            yield kind, start + header, start + size
            start += size

    def entries(start, end, depth=0):
        if depth > 6:
            return set()
        codecs = set()
        for kind, content, limit in boxes(start, end):
            if kind in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
                codecs.update(entries(content, limit, depth + 1))
            elif kind == b"stsd" and content + 8 <= limit:
                codecs.update(item[0] for item in boxes(content + 8, limit))
        return codecs

    return entries(0, len(data))


@dataclass
class RelayConnection:
    client: httpx.AsyncClient
    response: httpx.Response
    length: int
    total: int

    async def close(self):
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class RelayAdapter(Protocol):
    async def inspect(self, source: dict, maximum: int) -> dict | None: ...
    async def open(self, source: dict, maximum: int, range_header: str | None = None) -> RelayConnection: ...


def byte_range(header, total):
    if not header:
        return 0, total - 1
    if len(header) > 128:
        raise AppException("INVALID_RANGE", "文件范围请求过长", 416)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()):
        raise AppException("INVALID_RANGE", "仅支持单个 bytes Range", 416)
    first, last = match.groups()
    start = int(first) if first else max(0, total - int(last))
    end = min(int(last), total - 1) if first and last else total - 1
    if start >= total or end < start:
        raise AppException("INVALID_RANGE", "文件范围无效", 416)
    return start, end


class PublicRelayAdapter:
    def __init__(self, transport_factory=public_transport):
        self.transport_factory = transport_factory

    async def request(self, source, range_header):
        client = httpx.AsyncClient(transport=self.transport_factory(), trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(30, connect=5, pool=5))
        response = None
        try:
            url = source["url"]
            headers = {key: value for key, value in source.get("headers", {}).items()
                       if key.lower() in {"user-agent", "referer", "accept"}}
            headers["Accept-Encoding"] = "identity"
            if range_header:
                headers["Range"] = range_header
            for _ in range(6):
                url = await validate_url(url)
                redaction = sensitive_http_urls.set(True)
                try:
                    response = await client.send(client.build_request("GET", url, headers=headers), stream=True)
                finally:
                    sensitive_http_urls.reset(redaction)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return client, response
                target = response.headers.get("location")
                await response.aclose()
                if not target:
                    break
                url = urljoin(url, target)
            raise AppException("DELIVERY_UNAVAILABLE", "下载来源重定向异常，请生成成品下载", 502)
        except BaseException:
            if response:
                await response.aclose()
            await client.aclose()
            raise

    @staticmethod
    def lengths(response):
        if response.status_code not in {200, 206} or response.headers.get("content-encoding", "identity") != "identity":
            raise AppException("DELIVERY_UNAVAILABLE", "下载来源已失效，请生成成品下载", 502)
        length = response.headers.get("content-length", "")
        if len(length) > 20 or not length.isdecimal() or int(length) <= 0:
            raise AppException("DELIVERY_UNAVAILABLE", "来源无法确认文件大小，请生成成品下载", 502)
        total = int(length)
        if response.status_code == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if not match or int(match[2]) - int(match[1]) + 1 != int(length) or int(match[2]) >= int(match[3]):
                raise AppException("DELIVERY_UNAVAILABLE", "来源返回无效文件范围", 502)
            total = int(match[3])
        return int(length), total

    async def inspect(self, source, maximum):
        client = response = None
        try:
            async with asyncio.timeout(15):
                client, response = await self.request(source, f"bytes=0-{PROBE_BYTES - 1}")
                length, total = self.lengths(response)
                if response.status_code == 206 and not response.headers["content-range"].startswith("bytes 0-"):
                    return None
                if total > maximum:
                    return None
                prefix = bytearray()
                async for chunk in response.aiter_raw(CHUNK_BYTES):
                    prefix.extend(chunk)
                    if len(prefix) >= PROBE_BYTES or mp4_codecs(prefix) == {b"avc1", b"mp4a"}:
                        break
                if len(prefix) < 12 or prefix[4:8] != b"ftyp" or mp4_codecs(prefix) != {b"avc1", b"mp4a"}:
                    return None
                etag = response.headers.get("etag", "")
                if etag.startswith("W/") or not re.fullmatch(r'"[^"\r\n]+"', etag):
                    etag = None
                direct = (urlsplit(str(response.url)).scheme == "https" and urlsplit(str(response.url)).hostname in settings.DOWNLOAD_DIRECT_HOSTS
                    and not source.get("headers") and response.headers.get("content-disposition", "").lower().startswith("attachment"))
                return {**source, "url": str(response.url), "size_bytes": total, "etag": etag,
                        "delivery_mode": "direct" if direct else "relay",
                        "range_supported": response.status_code == 206 and etag is not None}
        except (AppException, httpx.HTTPError, TimeoutError):
            return None
        finally:
            if response is not None:
                await response.aclose()
            if client is not None:
                await client.aclose()

    async def open(self, source, maximum, range_header=None):
        client = response = None
        try:
            if range_header and not source["range_supported"]:
                raise AppException("DELIVERY_RANGE_UNAVAILABLE", "此来源暂不支持续传，请重新下载或生成成品", 409)
            start, end = byte_range(range_header, source["size_bytes"])
            async with asyncio.timeout(15):
                client, response = await self.request(source, f"bytes={start}-{end}" if range_header else None)
            length, total = self.lengths(response)
            if total != source["size_bytes"] or total > maximum or (source["etag"] and response.headers.get("etag") != source["etag"]):
                raise AppException("DELIVERY_CHANGED", "来源文件已变化，请生成成品下载", 409)
            if range_header and (response.status_code != 206 or response.headers.get("content-range") != f"bytes {start}-{end}/{total}"):
                raise AppException("DELIVERY_RANGE_UNAVAILABLE", "来源未正确响应续传请求，请重新下载", 409)
            if not range_header and response.status_code != 200:
                raise AppException("DELIVERY_UNAVAILABLE", "来源返回不完整文件，请生成成品下载", 502)
            return RelayConnection(client, response, length, total)
        except BaseException:
            if response is not None:
                await response.aclose()
            if client is not None:
                await client.aclose()
            raise
