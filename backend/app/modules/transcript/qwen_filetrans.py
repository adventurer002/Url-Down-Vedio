"""Bounded DashScope Filetrans REST adapter with real sentence timestamps.

Uses the documented inference upload policy for local verification. Uploaded
objects expire at the provider after 48 hours. No credentials or signed URLs
are persisted in tasks, transcripts, or logs.
"""

import asyncio
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from app.core.exceptions import AppException
from app.core.security import normalize_url
from app.infrastructure.http_client import public_transport
from app.modules.transcript.data import TranscriptData, make_transcript


class QwenFiletransASRProvider:
    precise_timestamps = True
    poll_interval = 0.5
    max_response_bytes = 8 * 1024 * 1024

    def __init__(
        self, base_url, api_key, model, *, timeout=180, max_upload_mb=25, transport=None
    ):
        base = base_url.rstrip("/")
        for suffix in ("/compatible-mode/v1", "/api/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base_url = self.public_url(base + "/api/v1")
        self.api_key, self.model = api_key, model
        self.available = bool(api_key)
        self.timeout, self.max_bytes = timeout, max_upload_mb * 1024 * 1024
        self.transport = transport

    @staticmethod
    def public_url(value, *, result=False):
        if not isinstance(value, str):
            raise AppException("ASR_INVALID_RESPONSE", "千问返回的文件地址无效", 502)
        normalize_url(value)
        parts = urlsplit(value)
        # Official result URLs can use HTTP. OSS signatures also work over TLS.
        if (
            result
            and parts.scheme == "http"
            and parts.hostname.endswith(".aliyuncs.com")
        ):
            value = urlunsplit(parts._replace(scheme="https"))
            parts = urlsplit(value)
        if parts.scheme != "https" or parts.fragment:
            raise AppException(
                "ASR_INVALID_RESPONSE", "千问文件地址需使用公开 HTTPS", 502
            )
        return value

    async def request(
        self,
        client,
        method,
        url,
        *,
        authenticated=False,
        retry=False,
        json_response=True,
        headers=None,
        **kwargs,
    ):
        # Only repeat safe reads. Upload/submission writes are never replayed
        # after a lost acknowledgement, which could create a second paid job.
        attempts = 2 if retry else 1
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(attempts):
            try:
                async with client.stream(
                    method, url, headers=request_headers, **kwargs
                ) as response:
                    if (
                        response.status_code in {429, 500, 502, 503, 504}
                        and attempt + 1 < attempts
                    ):
                        await asyncio.sleep(0.5)
                        continue
                    if response.status_code in {401, 403}:
                        raise AppException(
                            "ASR_AUTH_ERROR",
                            "千问鉴权失败，请检查密钥、地域和模型权限",
                            502,
                        )
                    if not 200 <= response.status_code < 300:
                        raise AppException(
                            "ASR_API_ERROR",
                            "千问文件转写请求失败，请检查模型、地址和服务额度",
                            502,
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise AppException(
                                "ASR_INVALID_RESPONSE", "千问转写响应超过大小限制", 502
                            )
                    if not json_response:
                        return None
                    value = json.loads(content)
                    if not isinstance(value, dict):
                        raise ValueError
                    return value
            except httpx.TimeoutException, httpx.TransportError:
                if attempt + 1 == attempts:
                    if method == "POST" and url.endswith("/asr/transcription"):
                        raise AppException(
                            "ASR_SUBMISSION_UNKNOWN",
                            "千问任务提交结果未确认，请核对服务商任务记录后再重试",
                            504,
                        ) from None
                    raise
                await asyncio.sleep(0.5)

    async def transcribe(
        self, audio_path: Path, language: str | None
    ) -> TranscriptData:
        if not self.available:
            raise AppException("ASR_NOT_CONFIGURED", "请配置千问 Filetrans 密钥", 503)
        size = audio_path.stat().st_size
        if not 0 < size <= self.max_bytes:
            raise AppException("ASR_AUDIO_TOO_LARGE", "音频为空或超过千问上传上限", 413)
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    transport=self.transport or public_transport(),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(30, connect=5, write=30, pool=5),
                ) as client:
                    uploaded = await self.upload(client, audio_path, size)
                    # Filetrans's word mode also splits sentences on punctuation.
                    # VAD-only mode can return an entire continuous lecture as
                    # one segment, which cannot locate a selected sentence.
                    parameters = {"channel_id": [0], "enable_words": True}
                    if language and language != "und":
                        parameters["language"] = language.split("-")[0]
                    reply = await self.request(
                        client,
                        "POST",
                        self.base_url + "/services/audio/asr/transcription",
                        authenticated=True,
                        headers={
                            "X-DashScope-Async": "enable",
                            "X-DashScope-OssResourceResolve": "enable",
                        },
                        json={
                            "model": self.model,
                            "input": {"file_url": uploaded},
                            "parameters": parameters,
                        },
                    )
                    task_id = reply["output"]["task_id"]
                    if not isinstance(task_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,128}", task_id
                    ):
                        raise ValueError
                    result_url = await self.wait_result(client, task_id)
                    result = await self.request(client, "GET", result_url, retry=True)
                    return self.transcript(result, language)
        except TimeoutError:
            raise AppException(
                "ASR_TIMEOUT", "千问文件上传或转写超过总执行时限", 504
            ) from None
        except httpx.TimeoutException:
            raise AppException(
                "ASR_TIMEOUT", "千问文件转写连接或读取超时", 504
            ) from None
        except httpx.TransportError:
            raise AppException(
                "ASR_API_ERROR", "千问文件转写网络请求失败", 502
            ) from None
        except ValueError, KeyError, TypeError, AttributeError, OverflowError:
            raise AppException(
                "ASR_INVALID_RESPONSE", "千问文件转写返回数据不完整或时间戳无效", 502
            ) from None

    async def upload(self, client, path, size):
        reply = await self.request(
            client,
            "GET",
            self.base_url + "/uploads",
            authenticated=True,
            retry=True,
            params={"action": "getPolicy", "model": self.model},
        )
        policy = reply["data"]
        if size > int(policy["max_file_size_mb"]) * 1024 * 1024:
            raise AppException(
                "ASR_AUDIO_TOO_LARGE", "音频超过百炼临时上传凭证的文件上限", 413
            )
        host = self.public_url(policy["upload_host"])
        directory = policy["upload_dir"]
        if not isinstance(directory, str) or not re.fullmatch(
            r"[A-Za-z0-9_/-]{1,1024}", directory
        ):
            raise ValueError
        suffix = path.suffix.lower()
        if suffix not in {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".aac"}:
            raise AppException(
                "ASR_AUDIO_FORMAT_INVALID", "千问文件转写不支持此音频格式", 422
            )
        filename = uuid4().hex + suffix
        key = directory.rstrip("/") + "/" + filename
        fields = {
            "OSSAccessKeyId": policy["oss_access_key_id"],
            "Signature": policy["signature"],
            "policy": policy["policy"],
            "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
            "key": key,
            "success_action_status": "200",
        }
        with path.open("rb") as audio:
            await self.request(
                client,
                "POST",
                host,
                json_response=False,
                data=fields,
                files={"file": (filename, audio, "application/octet-stream")},
            )
        return "oss://" + key

    async def wait_result(self, client, task_id):
        while True:
            reply = await self.request(
                client,
                "GET",
                self.base_url + "/tasks/" + task_id,
                authenticated=True,
                retry=True,
            )
            output = reply["output"]
            if output.get("task_id", task_id) != task_id:
                raise ValueError
            status = output["task_status"]
            if status == "SUCCEEDED":
                return self.public_url(
                    output["result"]["transcription_url"], result=True
                )
            if status in {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}:
                raise AppException(
                    "ASR_TASK_FAILED",
                    "千问转写任务失败，请检查文件格式、模型权限和服务额度",
                    502,
                )
            if status not in {"PENDING", "RUNNING"}:
                raise ValueError
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def transcript(result, language):
        tracks = result["transcripts"]
        if not isinstance(tracks, list) or len(tracks) != 1:
            raise ValueError
        sentences = tracks[0]["sentences"]
        if not isinstance(sentences, list) or not 1 <= len(sentences) <= 20000:
            raise ValueError
        segments = []
        detected = language
        previous_end = 0
        for sentence in sentences:
            start, end = sentence["begin_time"], sentence["end_time"]
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start < previous_end
                or end <= start
            ):
                raise ValueError
            text = sentence["text"]
            if not isinstance(text, str):
                raise ValueError
            segments.append({"start": start / 1000, "end": end / 1000, "text": text})
            previous_end = end
            if not detected or detected == "und":
                detected = sentence.get("language") or "und"
        return make_transcript(detected or "und", segments)
