"""Bounded S3-compatible object I/O using separate upload and read endpoints."""
import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import lru_cache
import hashlib
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings
from app.core.exceptions import AppException

CHUNK_BYTES = 256 * 1024
SIGNED_URL_SECONDS = 120


class ObjectStore(Protocol):
    tag: str

    async def put(self, key: str, path: Path, size: int, mime_type: str) -> None: ...
    def read(self, key: str, byte_range: str | None) -> AbstractAsyncContextManager[httpx.Response]: ...
    def sign_download(self, key: str, filename: str, expires_in: int) -> str: ...


def trusted_origin(value):
    try:
        parts = urlsplit(value)
        if (any(char.isspace() or char == "\\" for char in value)
                or parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment):
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        return value.rstrip("/")
    except ValueError:
        raise AppException("OBJECT_CONFIG_INVALID", "对象存储服务地址配置无效", 503) from None


def store_tag():
    namespace, bucket, region = settings.OBJECT_NAMESPACE, settings.OBJECT_BUCKET, settings.OBJECT_REGION
    if not all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}", part) for part in (namespace, bucket, region)):
        raise AppException("OBJECT_CONFIG_INVALID", "对象存储命名空间、桶或地域配置无效", 503)
    return hashlib.sha256(f"{namespace}/{bucket}/{region}".encode()).hexdigest()


def object_key(tag, storage_key):
    if not re.fullmatch(r"files/[a-f0-9-]{36}\.(mp4|mp3)", storage_key):
        raise AppException("OBJECT_KEY_INVALID", "对象文件标识无效", 503)
    return f"videohub-{tag[:16]}/{storage_key}"


async def file_chunks(file) -> AsyncIterator[bytes]:
    while chunk := await asyncio.to_thread(file.read, CHUNK_BYTES):
        yield chunk


class S3ObjectStore:
    def __init__(self, bucket, tag, *, client, transport=None):
        self.bucket, self.tag, self.client, self.transport = bucket, tag, client, transport

    def _sign(self, operation, key, *, expires_in=SIGNED_URL_SECONDS, **values):
        from botocore.exceptions import BotoCoreError

        try:
            return self.client.generate_presigned_url(operation,
                Params={"Bucket": self.bucket, "Key": key, **values}, ExpiresIn=expires_in)
        except (BotoCoreError, ValueError):
            raise AppException("OBJECT_CONFIG_INVALID", "对象存储签名配置无效", 503) from None

    def sign_download(self, key, filename, expires_in):
        return self._sign("get_object", key, expires_in=expires_in,
                          ResponseContentDisposition=f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                          ResponseCacheControl="private, no-store")

    def _http(self):
        return httpx.AsyncClient(transport=self.transport, follow_redirects=False, trust_env=False,
                                 timeout=httpx.Timeout(30, connect=5, write=30, pool=5))

    async def put(self, key, path, size, mime_type):
        try:
            async with asyncio.timeout(settings.OBJECT_UPLOAD_TIMEOUT_SECONDS), self._http() as client:
                url = self._sign("put_object", key, ContentLength=size, ContentType=mime_type)
                with path.open("rb") as file:
                    async with client.stream("PUT", url, content=file_chunks(file), headers={
                        "Content-Length": str(size), "Content-Type": mime_type,
                    }) as response:
                        response.raise_for_status()
                head = await client.head(self._sign("head_object", key))
                if head.status_code != 200 or head.headers.get("content-length") != str(size):
                    raise AppException("OBJECT_NOT_VERIFIED", "对象上传后未通过大小校验", 502)
        except (TimeoutError, httpx.HTTPError, OSError):
            raise AppException("OBJECT_UPLOAD_FAILED", "对象上传失败或超时", 502) from None

    @asynccontextmanager
    async def read(self, key, byte_range):
        params = {"Range": byte_range} if byte_range else {}
        try:
            async with self._http() as client:
                async with client.stream("GET", self._sign("get_object", key, **params),
                        headers={"Accept-Encoding": "identity", **params}) as response:
                    yield response
        except httpx.HTTPError:
            raise AppException("OBJECT_READ_FAILED", "对象下载失败", 502) from None


@lru_cache(maxsize=8)
def _adapter(endpoint, tag, bucket, region, key, secret, session_token):
    import boto3
    from botocore.config import Config

    # COS regional endpoints require the bucket in the hostname for new buckets.
    addressing_style = "virtual" if urlsplit(endpoint).hostname == f"cos.{region}.myqcloud.com" else "path"
    client = boto3.client("s3", endpoint_url=endpoint, region_name=region,
                          aws_access_key_id=key, aws_secret_access_key=secret,
                          aws_session_token=session_token or None,
                          config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style},
                                        request_checksum_calculation="when_required",
                                        retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=30))
    return S3ObjectStore(bucket, tag, client=client)


def object_store(*, upload=False) -> ObjectStore:
    endpoint = trusted_origin(settings.OBJECT_UPLOAD_ENDPOINT if upload else settings.OBJECT_READ_ENDPOINT)
    key, secret = settings.OBJECT_ACCESS_KEY.get_secret_value(), settings.OBJECT_SECRET_KEY.get_secret_value()
    if not key or not secret:
        raise AppException("OBJECT_CONFIG_INVALID", "对象存储缺少服务端凭据", 503)
    return _adapter(endpoint, store_tag(), settings.OBJECT_BUCKET, settings.OBJECT_REGION,
                    key, secret, settings.OBJECT_SESSION_TOKEN.get_secret_value())
