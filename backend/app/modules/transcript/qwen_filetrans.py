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
        self.audio_filetrans = model.startswith("qwen-audio-")
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
            stage = "获取上传凭证或上传音频"
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    transport=self.transport or public_transport(),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(30, connect=5, write=30, pool=5),
                ) as client:
                    uploaded = await self.upload(client, audio_path, size)
                    parameters = {"channel_id": [0]}
                    if not self.audio_filetrans:
                        # Qwen3 needs word mode for punctuation sentence boundaries.
                        parameters["enable_words"] = True
                    if language and language != "und":
                        code = language.split("-")[0]
                        if self.audio_filetrans:
                            parameters["language_hints"] = [code]
                        else:
                            parameters["language"] = code
                    source = (
                        {"file_urls": [uploaded]}
                        if self.audio_filetrans
                        else {"file_url": uploaded}
                    )
                    stage = "提交转写任务"
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
                            "input": source,
                            "parameters": parameters,
                        },
                    )
                    task_id = reply["output"]["task_id"]
                    if not isinstance(task_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,128}", task_id
                    ):
                        raise ValueError
                    stage = "查询转写任务"
                    result_url = await self.wait_result(client, task_id)
                    stage = "读取转写文本和时间戳"
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
                "ASR_INVALID_RESPONSE", f"千问在{stage}时返回数据不完整或格式无效", 502
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
                if self.audio_filetrans:
                    results = output["results"]
                    if not isinstance(results, list) or len(results) != 1:
                        raise ValueError
                    result = results[0]
                    if result["subtask_status"] != "SUCCEEDED":
                        raise AppException(
                            "ASR_TASK_FAILED",
                            "千问音频子任务失败，请检查文件格式、模型权限和服务额度",
                            502,
                        )
                else:
                    result = output["result"]
                return self.public_url(result["transcription_url"], result=True)
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
            segments.extend(QwenFiletransASRProvider.sentence_segments(sentence))
            previous_end = end
            if not detected or detected == "und":
                detected = sentence.get("language") or "und"
        return make_transcript(detected or "und", segments)

    @staticmethod
    def sentence_segments(sentence):
        """Use verified word times for clause boundaries inside a vendor sentence."""
        words = sentence.get("words")
        if words is None:
            return [{"start": sentence["begin_time"] / 1000,
                     "end": sentence["end_time"] / 1000, "text": sentence["text"]}]
        if not isinstance(words, list) or not 1 <= len(words) <= 20000:
            raise ValueError
        previous_end = sentence["begin_time"]
        pieces = []
        for word in words:
            start, end = word["begin_time"], word["end_time"]
            text, punctuation = word["text"], word.get("punctuation", "")
            if (
                isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous_end or end <= start
                or end > sentence["end_time"]
                or not isinstance(text, str) or not text.strip()
                or not isinstance(punctuation, str)
            ):
                raise ValueError
            pieces.append(text + punctuation)
            previous_end = end
        # Word boundaries are usable only when they account for the entire sentence.
        if "".join("".join(pieces).split()) != "".join(sentence["text"].split()):
            raise ValueError
        segments, current = [], []
        clause_start = words[0]["begin_time"]
        for index, word in enumerate(words):
            current.append(pieces[index])
            punctuation = word.get("punctuation", "")
            next_text = words[index + 1]["text"] if index + 1 < len(words) else ""
            decimal = (punctuation in {".", ","} and word["text"][-1:].isdigit()
                       and next_text[:1].isdigit())
            if index + 1 == len(words) or (
                re.search(r"[,，。.!?！？;；:：]", punctuation) and not decimal
            ):
                segments.append({"start": clause_start / 1000,
                                 "end": word["end_time"] / 1000,
                                 "text": "".join(current)})
                current = []
                if index + 1 < len(words):
                    clause_start = words[index + 1]["begin_time"]
        return segments
