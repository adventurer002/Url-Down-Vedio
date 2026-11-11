"""One resolver for API and workers. Task payloads contain immutable IDs, never secrets."""

from decimal import Decimal
import asyncio, time
from contextvars import ContextVar
import hashlib, json
from uuid import UUID
from sqlalchemy import select, delete
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.model_config.models import (
    ModelConfigState,
    ModelRelease,
    ModelRevision,
    ModelCredential,
)
from app.modules.model_config.security import decrypt
from app.modules.transcript.providers import CLOUD_ASR_PROVIDERS

probe_context = ContextVar("model_probe", default=False)


async def observe(identifier, factory, purpose, operation):
    started = time.monotonic()
    success = False
    code = None
    try:
        value = await operation()
        success = True
        return value
    except BaseException as error:
        code = (
            error.code
            if isinstance(error, AppException)
            else "TASK_CANCELLED"
            if isinstance(error, asyncio.CancelledError)
            else "MODEL_CALL_FAILED"
        )
        raise
    finally:
        duration = round((time.monotonic() - started) * 1000)

        def write():
            from app.modules.model_config.models import ModelCall
            from app.modules.billing.policy import now
            from datetime import timedelta
            from uuid import uuid4

            identifier_uuid = uuid4()
            with (factory or sessions()).begin() as session:
                session.add(
                    ModelCall(
                        id=identifier_uuid,
                        revision_id=UUID(str(identifier)),
                        scope="test" if probe_context.get() else "runtime",
                        purpose=purpose,
                        success=success,
                        duration_ms=duration,
                        error_code=code,
                    )
                )
                if identifier_uuid.int % 100 == 0:
                    stale = (
                        select(ModelCall.id)
                        .where(ModelCall.created_at < now() - timedelta(days=30))
                        .limit(500)
                    )
                    session.execute(delete(ModelCall).where(ModelCall.id.in_(stale)))

        try:
            async with asyncio.timeout(1.5):
                await asyncio.to_thread(write)
        except Exception, asyncio.CancelledError:
            pass


USES = (
    "summary",
    "mindmap",
    "question",
    "clip_analysis",
    "transcription",
    "clip_timing",
)
TASK_USE = {
    "ai_summary": "summary",
    "ai_mindmap": "mindmap",
    "ai_question": "question",
    "ai_clip_plan": "clip_analysis",
    "transcription": "transcription",
    "clips_export": "clip_timing",
}


def sessions():
    from app.infrastructure.database.session import SessionLocal

    return SessionLocal


def capture(session, lock=False):
    if not settings.MODEL_CONFIG_ENABLED:
        return None
    query = select(ModelConfigState).where(ModelConfigState.id == 1)
    if lock:
        query = query.with_for_update(read=True)
    state = session.scalar(query)
    if not state or not state.release_id:
        return None
    release = session.get(ModelRelease, state.release_id)
    return {"release_id": str(release.id), "bindings": dict(release.bindings)}


def validate_snapshot(session, payload):
    frozen = payload.get("model_config")
    if not frozen and not settings.MODEL_CONFIG_ENABLED:
        return
    current = capture(session, lock=True)
    if not frozen and not current:
        return
    if not current or current != frozen:
        raise AppException(
            "MODEL_CONFIGURATION_CHANGED", "模型配置已更新，请重新确认处理方案", 409
        )


def reference(use, snapshot=None, factory=None):
    if snapshot is not None:
        return snapshot["bindings"].get(use), True
    if not settings.MODEL_CONFIG_ENABLED:
        return None, False
    with (factory or sessions())() as session:
        current = capture(session)
        if current is None:
            return None, False
        return current["bindings"].get(use), True


def configuration(identifier, factory=None):
    with (factory or sessions())() as session:
        revision = session.get(ModelRevision, UUID(str(identifier)))
        if revision is None or "connection_revision" not in revision.payload:
            raise AppException("MODEL_CONFIG_NOT_FOUND", "模型配置版本不存在", 503)
        connection = session.get(
            ModelRevision, UUID(revision.payload["connection_revision"])
        )
        credential = (
            session.get(ModelCredential, UUID(connection.payload["credential_id"]))
            if connection
            else None
        )
        if not credential or credential.revoked:
            raise AppException(
                "MODEL_CREDENTIAL_REVOKED", "模型密钥已撤销，请联系管理员", 503
            )
        return (
            dict(revision.payload),
            dict(connection.payload),
            decrypt(credential.ciphertext),
        )


class ConfiguredLLM:
    name = "openai_compatible"

    def __init__(self, identifier, factory=None):
        self.identifier, self.factory = str(identifier), factory
        model, _, _ = configuration(identifier, factory)
        self._identity = "cfg_" + UUID(self.identifier).hex
        self.input_rate = (
            Decimal(model["input_price"])
            if model.get("input_price") is not None
            else None
        )
        self.output_rate = (
            Decimal(model["output_price"])
            if model.get("output_price") is not None
            else None
        )
        self.cost_rates = (self.input_rate, self.output_rate)

    @property
    def model(self):
        return self._identity

    @model.setter
    def model(self, value):
        if value != self._identity:
            raise AppException(
                "MODEL_CONFIGURATION_CHANGED", "任务模型版本不匹配，请重新确认", 409
            )

    async def generate(self, request):
        async def operation():
            from app.modules.ai.providers.openai_compatible import (
                OpenAICompatibleLLMProvider,
            )

            model, connection, key = configuration(self.identifier, self.factory)
            return await OpenAICompatibleLLMProvider(
                connection["base_url"],
                key,
                model["model_id"],
                timeout=model["timeout"],
                max_tokens=model["max_tokens"],
            ).generate(request)

        return await observe(self.identifier, self.factory, request.purpose, operation)


class ConfiguredASR:
    def __init__(self, identifier, factory=None):
        self.identifier, self.factory = str(identifier), factory
        _, connection, _ = configuration(identifier, factory)
        self.precise_timestamps = CLOUD_ASR_PROVIDERS[
            connection["adapter"]
        ].precise_timestamps
        self._identity = "cfg_" + UUID(self.identifier).hex
        self.available = True

    @property
    def model(self):
        return self._identity

    @model.setter
    def model(self, value):
        if value != self._identity:
            raise AppException(
                "MODEL_CONFIGURATION_CHANGED", "任务转写版本不匹配，请重新确认", 409
            )

    async def transcribe(self, path, language):
        async def operation():
            model, connection, key = configuration(self.identifier, self.factory)
            adapter = CLOUD_ASR_PROVIDERS[connection["adapter"]]
            return await adapter(
                connection["base_url"],
                key,
                model["model_id"],
                timeout=model["timeout"],
                max_upload_mb=model["max_upload_mb"],
            ).transcribe(path, language)

        return await observe(self.identifier, self.factory, "transcription", operation)


class RoutedMindmap:
    name = "openai_compatible"

    def __init__(self, summary, mindmap):
        self.summary, self.mindmap = summary, mindmap
        self._identity = (
            "map_"
            + hashlib.sha256(
                json.dumps([summary.model, mindmap.model]).encode()
            ).hexdigest()
        )
        self.summary_model = summary.model
        self.cost_rates = mindmap.cost_rates
        self.summary_cost_rates = summary.cost_rates

    @property
    def model(self):
        return self._identity

    @model.setter
    def model(self, value):
        if value != self._identity:
            raise AppException(
                "MODEL_CONFIGURATION_CHANGED", "导图任务模型版本不匹配", 409
            )

    async def generate(self, request):
        return await (
            self.mindmap if request.purpose == "mindmap" else self.summary
        ).generate(request)


def llm(use="summary", snapshot=None, factory=None):
    if snapshot is None and settings.MODEL_CONFIG_ENABLED:
        with (factory or sessions())() as session:
            snapshot = capture(session)
        if snapshot is None:
            return None
    identifier, managed = reference(use, snapshot, factory)
    if not managed:
        return None
    if not identifier:
        raise AppException("MODEL_NOT_ASSIGNED", "此功能尚未分配模型", 503)
    provider = ConfiguredLLM(identifier, factory)
    if use == "mindmap":
        summary_id, _ = reference("summary", snapshot, factory)
        if summary_id and summary_id != identifier:
            return RoutedMindmap(ConfiguredLLM(summary_id, factory), provider)
    return provider


def asr(use="transcription", snapshot=None, factory=None):
    identifier, managed = reference(use, snapshot, factory)
    if not managed:
        return None
    if not identifier:
        from app.modules.transcript.providers import FallbackLocalASRProvider

        return FallbackLocalASRProvider(model="disabled-v1")
    return ConfiguredASR(identifier, factory)
