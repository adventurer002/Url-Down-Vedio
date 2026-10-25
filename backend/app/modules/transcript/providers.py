import asyncio
import base64
from pathlib import Path
import json
import shutil
import tempfile
import wave

from app.infrastructure.process_runner import ProcessRunner
from typing import Protocol

import httpx

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import normalize_url
from app.infrastructure.http_client import PublicNetworkBackend, public_transport
from app.modules.transcript.data import TranscriptData, make_transcript


class ASRProvider(Protocol):
    model: str
    available: bool

    async def transcribe(
        self, audio_path: Path, language: str | None
    ) -> TranscriptData: ...


class OpenAICompatibleASRProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 90,
        max_upload_mb: int = 25,
        transport=None,
    ):
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.api_key, self.model = api_key, model
        self.available = bool(api_key)
        self.timeout, self.max_bytes = timeout, max_upload_mb * 1024 * 1024
        self.transport = transport

    async def transcribe(
        self, audio_path: Path, language: str | None
    ) -> TranscriptData:
        if not self.api_key:
            raise AppException(
                "ASR_NOT_CONFIGURED", "请配置云端 ASR 密钥或显式启用离线 Mock", 503
            )
        normalize_url(self.url)
        if audio_path.stat().st_size > self.max_bytes:
            raise AppException(
                "ASR_AUDIO_TOO_LARGE", "音频超过云端 ASR 上传上限，请使用较短资源", 413
            )
        transport = self.transport
        if transport is None:
            transport = public_transport()
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(60, connect=5, write=30, pool=5),
                ) as client:
                    for attempt in range(2):
                        try:
                            fields = {
                                "model": self.model,
                                "response_format": "verbose_json",
                                "timestamp_granularities[]": "segment",
                            }
                            if language:
                                fields["language"] = language.split("-")[0]
                            with audio_path.open("rb") as audio:
                                async with client.stream(
                                    "POST",
                                    self.url,
                                    headers={"Authorization": f"Bearer {self.api_key}"},
                                    data=fields,
                                    files={"file": ("audio.mp3", audio, "audio/mpeg")},
                                ) as response:
                                    if (
                                        response.status_code
                                        in {429, 500, 502, 503, 504}
                                        and attempt == 0
                                    ):
                                        await asyncio.sleep(0.5)
                                        continue
                                    if response.status_code >= 300:
                                        raise AppException(
                                            "ASR_API_ERROR",
                                            "云端 ASR 请求失败，请检查服务端配置或额度",
                                            502,
                                        )
                                    body = bytearray()
                                    async for chunk in response.aiter_bytes():
                                        body.extend(chunk)
                                        if len(body) > 2 * 1024 * 1024:
                                            raise AppException(
                                                "ASR_INVALID_RESPONSE",
                                                "ASR 响应超过大小限制",
                                                502,
                                            )
                            payload = json.loads(body)
                            return make_transcript(
                                language or payload.get("language") or "und",
                                payload["segments"],
                            )
                        except httpx.TimeoutException:
                            if attempt:
                                raise AppException(
                                    "ASR_TIMEOUT", "云端 ASR 连接或读取超时", 504
                                ) from None
                            await asyncio.sleep(0.5)
                        except httpx.TransportError:
                            if attempt:
                                raise AppException(
                                    "ASR_API_ERROR", "云端 ASR 网络请求失败", 502
                                ) from None
                            await asyncio.sleep(0.5)
        except TimeoutError:
            raise AppException("ASR_TIMEOUT", "云端 ASR 超过总执行时限", 504) from None
        except (ValueError, KeyError, TypeError):
            raise AppException(
                "ASR_INVALID_RESPONSE", "ASR 响应缺少有效的分段时间轴", 502
            ) from None


class QwenASRProvider:
    """阿里云百炼 Qwen ASR。

    支持两代模型：
    - qwen3-asr-flash 等老模型：走 OpenAI 兼容 /chat/completions，messages 放顶层，
      响应文本在 choices[0].message.content。
    - qwen-audio-3.x-asr-flash 新模型：走 DashScope 原生 multimodal-generation 端点，
      messages 包在 input 内，需 parameters.format，响应文本在 output.sentence.text。
    音频按最多 120 秒切片；每段时间范围来自真实音频长度，并非逐句对齐时间戳。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 90,
        max_upload_mb: int = 25,
        transport=None,
    ):
        self.model = model
        # qwen-audio-3.x-asr-flash 系列走 DashScope 原生端点；其余走 OpenAI 兼容端点。
        if model.startswith("qwen-audio-"):
            base = base_url.rstrip("/")
            if base.endswith("/compatible-mode/v1"):
                base = base[: -len("/compatible-mode/v1")]
            self.url = base + "/api/v1/services/aigc/multimodal-generation/generation"
            self._is_new_api = True
        else:
            self.url = base_url.rstrip("/") + "/chat/completions"
            self._is_new_api = False
        self.api_key = api_key
        self.available = bool(api_key)
        self.timeout, self.max_bytes = timeout, max_upload_mb * 1024 * 1024
        self.transport = transport

    async def transcribe(
        self, audio_path: Path, language: str | None
    ) -> TranscriptData:
        if not self.available:
            raise AppException("ASR_NOT_CONFIGURED", "请配置千问 ASR 密钥", 503)
        # The upload limit applies to each request, not the original recording.
        # Keep the separate task file-size protection for the source itself.
        if audio_path.stat().st_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise AppException("FILE_TOO_LARGE", "音频超过任务文件体积上限", 413)
        # Mono 16 kHz signed 16-bit PCM is 32,000 bytes/s. Reserve space for
        # WAV headers and the encoder's segment boundary rounding.
        segment_seconds = min(120, (self.max_bytes - 4096) // 32000)
        if segment_seconds < 1:
            raise AppException(
                "ASR_AUDIO_TOO_LARGE",
                "ASR 单次上传限制不足以容纳音频分段，请联系管理员",
                413,
            )
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AppException("FFMPEG_UNAVAILABLE", "千问分段转写需要 FFmpeg", 503)
        try:
            async with asyncio.timeout(self.timeout):
                with tempfile.TemporaryDirectory(
                    prefix="asr-chunks-", dir=audio_path.parent
                ) as temporary:
                    directory = Path(temporary)

                    async def cancelled():
                        return False  # The parent cooperative_asr cancels this coroutine every 500ms.

                    async def progress(event):
                        pass

                    await ProcessRunner(
                        min(30, self.timeout), settings.MAX_FILE_SIZE_MB * 1024 * 1024
                    ).run(
                        [
                            ffmpeg,
                            "-nostdin",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-protocol_whitelist",
                            "file,pipe",
                            "-i",
                            str(audio_path),
                            "-map",
                            "0:a:0",
                            "-vn",
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            "-threads",
                            "1",
                            "-c:a",
                            "pcm_s16le",
                            "-f",
                            "segment",
                            "-segment_time",
                            str(segment_seconds),
                            "-reset_timestamps",
                            "1",
                            str(directory / "chunk-%05d.wav"),
                        ],
                        directory,
                        cwd=directory,
                        is_cancel_requested=cancelled,
                        on_progress=progress,
                    )
                    paths = sorted(directory.glob("chunk-*.wav"))
                    if not paths:
                        raise AppException(
                            "ASR_INVALID_RESPONSE", "未能生成转写音频分段", 502
                        )
                    segments, offset, detected = [], 0.0, language
                    for path in paths:
                        with wave.open(str(path), "rb") as audio:
                            duration = audio.getnframes() / audio.getframerate()
                        data = await self._transcribe_chunk(path, language)
                        segments.append(
                            {
                                "start": offset,
                                "end": offset + duration,
                                "text": data.content_text,
                            }
                        )
                        offset += duration
                        detected = detected or data.language
                    return make_transcript(detected or "und", segments)
        except TimeoutError:
            raise AppException(
                "ASR_TIMEOUT", "千问分段转写超过总执行时限", 504
            ) from None

    async def _transcribe_chunk(
        self, audio_path: Path, language: str | None
    ) -> TranscriptData:
        if not self.api_key:
            raise AppException(
                "ASR_NOT_CONFIGURED", "请配置云端 ASR 密钥或显式启用离线 Mock", 503
            )
        normalize_url(self.url)
        if audio_path.stat().st_size > self.max_bytes:
            raise AppException(
                "ASR_AUDIO_TOO_LARGE", "音频超过云端 ASR 上传上限，请使用较短资源", 413
            )
        transport = self.transport or public_transport()
        suffix = audio_path.suffix.lower().lstrip(".") or "mp3"
        mime = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
        }.get(audio_path.suffix.lower(), "audio/mpeg")
        audio_data_uri = (
            "data:"
            + mime
            + ";base64,"
            + base64.b64encode(audio_path.read_bytes()).decode("ascii")
        )
        if self._is_new_api:
            # qwen-audio-3.x-asr-flash：DashScope 原生格式，messages 包在 input 内，必须带 parameters.format
            payload: dict = {
                "model": self.model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": audio_data_uri},
                                }
                            ],
                        }
                    ]
                },
                "parameters": {
                    "format": suffix,
                    "sample_rate": "16000",
                },
            }
            if language:
                payload["parameters"]["language_hints"] = [language.split("-")[0]]
        else:
            # qwen3-asr-flash 等老模型：OpenAI 兼容 chat/completions
            payload = {
                "model": self.model,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "format": suffix,
                                    "data": audio_data_uri,
                                },
                            }
                        ],
                    }
                ],
            }
            if language:
                payload["asr_options"] = {"language": language.split("-")[0]}
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(60, connect=5, write=30, pool=5),
                ) as client:
                    for attempt in range(2):
                        try:
                            response = await client.post(
                                self.url,
                                headers={"Authorization": f"Bearer {self.api_key}"},
                                json=payload,
                            )
                            if (
                                response.status_code in {429, 500, 502, 503, 504}
                                and attempt == 0
                            ):
                                await asyncio.sleep(0.5)
                                continue
                            if response.status_code >= 300:
                                try:
                                    error = response.json().get("error") or {}
                                    code = (
                                        error.get("code")
                                        if isinstance(error, dict)
                                        else None
                                    )
                                except (ValueError, AttributeError):
                                    code = None
                                if code == "UNSUPPORTED_FORMAT":
                                    raise AppException(
                                        "ASR_AUDIO_FORMAT_UNSUPPORTED",
                                        "千问不支持当前音频格式或模型请求格式，请检查 ASR_MODEL 与音频参数",
                                        502,
                                    )
                                if code in {
                                    "model_not_supported",
                                    "model_not_found",
                                    "ModelNotFound",
                                }:
                                    raise AppException(
                                        "ASR_MODEL_UNSUPPORTED",
                                        "千问 ASR 模型不存在或不受支持，请检查 ASR_MODEL",
                                        502,
                                    )
                                if response.status_code in {401, 403}:
                                    raise AppException(
                                        "ASR_AUTH_ERROR",
                                        "千问 ASR 密钥无效或没有模型访问权限",
                                        502,
                                    )
                                if response.status_code == 404:
                                    raise AppException(
                                        "ASR_ENDPOINT_NOT_FOUND",
                                        "千问 ASR 接口不存在，请检查服务地址与模型",
                                        502,
                                    )
                                if response.status_code == 429:
                                    raise AppException(
                                        "ASR_RATE_LIMITED",
                                        "千问 ASR 请求受限，请检查额度或稍后重试",
                                        502,
                                    )
                                raise AppException(
                                    "ASR_API_ERROR",
                                    "千问 ASR 请求失败，请检查服务端配置",
                                    502,
                                )
                            data = response.json()
                            if self._is_new_api:
                                # 新模型：output.sentence.text / output.text
                                output = data.get("output") or {}
                                sentence = output.get("sentence") or {}
                                text = sentence.get("text") or output.get("text") or ""
                                if not isinstance(text, str) or not text.strip():
                                    raise ValueError
                                begin = (sentence.get("begin_time") or 0) / 1000.0
                                end = (sentence.get("end_time") or 0) / 1000.0
                                if end <= begin:
                                    end = begin + 1
                                return make_transcript(
                                    language or "und",
                                    [{"start": begin, "end": end, "text": text}],
                                )
                            text = data["choices"][0]["message"]["content"]
                            if isinstance(text, list):
                                text = "".join(
                                    (
                                        item.get("text", "")
                                        if isinstance(item, dict)
                                        else str(item)
                                    )
                                    for item in text
                                )
                            if not isinstance(text, str) or not text.strip():
                                raise ValueError
                            detected = language or next(
                                (
                                    item.get("language")
                                    for item in data["choices"][0]["message"].get("annotations") or []
                                    if isinstance(item, dict) and item.get("language")
                                ),
                                None,
                            )
                            return make_transcript(
                                detected or "und",
                                [{"start": 0, "end": 1, "text": text}],
                            )
                        except httpx.TimeoutException:
                            if attempt:
                                raise AppException(
                                    "ASR_TIMEOUT", "云端 ASR 连接或读取超时", 504
                                ) from None
                            await asyncio.sleep(0.5)
                        except httpx.TransportError:
                            if attempt:
                                raise AppException(
                                    "ASR_API_ERROR", "云端 ASR 网络请求失败", 502
                                ) from None
                            await asyncio.sleep(0.5)
        except TimeoutError:
            raise AppException("ASR_TIMEOUT", "云端 ASR 超过总执行时限", 504) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise AppException(
                "ASR_INVALID_RESPONSE", "ASR 响应缺少有效的转写文本", 502
            ) from None


class FallbackLocalASRProvider:
    def __init__(
        self,
        *,
        mock: bool = False,
        model: str = "whisper-1",
        text: str = "离线模拟转写内容（非真实识别结果）",
    ):
        self.mock, self.text = mock, text
        self.available = mock
        self.model = "mock-v1" if mock else model

    async def transcribe(
        self, audio_path: Path, language: str | None
    ) -> TranscriptData:
        if not self.mock:
            raise AppException(
                "ASR_NOT_CONFIGURED",
                "未配置云端 ASR；默认不加载本地大模型。可配置密钥或显式设置 ASR_PROVIDER=mock",
                503,
            )
        return make_transcript(
            language or "und", [{"start": 0, "end": 1, "text": self.text}]
        )


def get_asr_provider() -> ASRProvider:
    if settings.ASR_PROVIDER == "mock":
        return FallbackLocalASRProvider(mock=True, text=settings.ASR_MOCK_TEXT)
    if settings.ASR_PROVIDER == "qwen":
        if settings.ASR_API_KEY.get_secret_value():
            return QwenASRProvider(
                settings.ASR_API_BASE_URL,
                settings.ASR_API_KEY.get_secret_value(),
                settings.ASR_MODEL,
                timeout=settings.ASR_TOTAL_TIMEOUT_SECONDS,
                max_upload_mb=settings.ASR_MAX_UPLOAD_MB,
            )
        return FallbackLocalASRProvider(model=settings.ASR_MODEL)
    if (
        settings.ASR_PROVIDER in {"auto", "openai_compatible"}
        and settings.ASR_API_KEY.get_secret_value()
    ):
        return OpenAICompatibleASRProvider(
            settings.ASR_API_BASE_URL,
            settings.ASR_API_KEY.get_secret_value(),
            settings.ASR_MODEL,
            timeout=settings.ASR_TOTAL_TIMEOUT_SECONDS,
            max_upload_mb=settings.ASR_MAX_UPLOAD_MB,
        )
    return FallbackLocalASRProvider(model=settings.ASR_MODEL)
