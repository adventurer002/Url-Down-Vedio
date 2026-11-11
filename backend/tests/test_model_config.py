"""Model configuration transactions, runtime pinning and probes; no live vendor requests."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock
import asyncio, json
import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, func
from test_admin import admin_data, bootstrap, headers
from test_tasks import repository, MemoryBus
from app.core.config import settings
from app.modules.admin.service import AdminService
from app.core.exceptions import AppException
from app.modules.model_config.models import (
    ModelResource,
    ModelRevision,
    ModelCredential,
    ModelConfigState,
    ModelProbe,
)
from app.modules.model_config.router import (
    ConnectionRequest,
    ModelRequest,
    ProbeRequest,
    PublishRequest,
    Operation,
    ArchiveRequest,
    get_model_config_service,
)
from app.modules.model_config.service import ModelConfigService
from app.modules.model_config import runtime
from app.modules.model_config.worker import execute_test, run_checks
from app.modules.tasks.service import TaskService
from app.modules.ai.providers.base import LLMRequest, LLMResult
from app.modules.auth.repository import UserRepository
from app.modules.auth.models import User
from app.modules.audit.models import AuditEvent
from app.modules.audit.repository import AuditRepository
from app.modules.billing.repository import AllowanceRepository


@pytest.fixture
def models(admin_data, monkeypatch):
    d = admin_data
    bootstrap(d)
    monkeypatch.setattr(settings, "MODEL_CONFIG_ENABLED", True)
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "MODEL_CONFIG_ENCRYPTION_KEY",
        __import__("pydantic").SecretStr(Fernet.generate_key().decode()),
    )
    monkeypatch.setattr(runtime, "sessions", lambda: d.sessions)
    service = ModelConfigService(d.sessions)
    actor = d.users.get(d.operator.id)

    def connection(
        name="Test connection",
        key="vendor-secret-test",
        adapter="openai_chat",
        **changes,
    ):
        return service.save(
            actor,
            ConnectionRequest(
                key=str(uuid4()),
                reason="fixture",
                name=name,
                adapter=adapter,
                base_url="https://api.example.com/v1",
                api_key=key,
                **changes,
            ),
            "req_test",
            "connection",
        )

    def model(connection_id, **changes):
        return service.save(
            actor,
            ModelRequest(
                key=str(uuid4()),
                reason="fixture",
                name="Test model",
                connection_revision=UUID(connection_id),
                model_id="upstream-v1",
                **changes,
            ),
            "req_test",
            "model",
        )

    def test(identifier, phase="compatibility", capabilities=None):
        body = ProbeRequest(
            key=str(uuid4()),
            reason="fixture",
            revision_id=UUID(identifier),
            phase=phase,
        )
        receipt = service.probe(actor, body, "req_test")
        task = d.tasks.create(
            "model_test",
            input_payload={
                "probe_id": receipt["probe_id"],
                "admin_version": actor.auth_version,
            },
            user_id=actor.id,
        )
        d.tasks.start(task.id)
        if capabilities is not False:
            d.tasks.complete_with(
                task.id,
                {
                    "capabilities": capabilities
                    or ["summary", "mindmap", "question", "clip_analysis"]
                },
                lambda session: None,
            )
        return task

    def publish(identifier, version=0, **changes):
        bindings = {
            use: identifier if use not in {"transcription", "clip_timing"} else None
            for use in runtime.USES
        }
        return service.publish(
            actor,
            PublishRequest(
                key=str(uuid4()),
                reason="publish",
                expected_version=version,
                bindings=bindings,
                **changes,
            ),
            "req_test",
        )

    d.app.dependency_overrides[get_model_config_service] = lambda: service
    return SimpleNamespace(
        d=d,
        service=service,
        actor=actor,
        connection=connection,
        model=model,
        test=test,
        publish=publish,
    )


def test_encrypted_keys_no_catalog_audit_leaks_and_conflicting_secret_retry(models):
    m = models
    body = ConnectionRequest(
        key="same-key",
        reason="fixture",
        name="connection",
        adapter="openai_chat",
        base_url="https://api.example.com/v1",
        api_key="never-expose-this-key",
    )
    first = m.service.save(m.actor, body, "req_test", "connection")
    assert m.service.save(m.actor, body, "req_test", "connection")["duplicate"]
    with pytest.raises(AppException) as error:
        m.service.save(
            m.actor,
            body.model_copy(
                update={"api_key": __import__("pydantic").SecretStr("different-key")}
            ),
            "req_test",
            "connection",
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert "never-expose" not in str(m.service.catalog())
    with m.d.sessions() as session:
        credential = session.scalar(select(ModelCredential))
        assert "never-expose" not in credential.ciphertext
        assert "never-expose" not in str(session.scalars(select(AuditEvent)).all())
        for event in session.scalars(select(AuditEvent)):
            assert "never-expose" not in str(event.request_data) + str(
                event.changes
            ) + str(event.result)
    assert (
        runtime.configuration(
            m.model(first["revision_id"])["revision_id"], m.d.sessions
        )[2]
        == "never-expose-this-key"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://api.example.com/v1?key=oops",
        "https://api.example.com/v1#oops",
        "https://user:password@example.com/v1",
    ],
)
def test_public_https_endpoints_only(models, url):
    m = models
    body = ConnectionRequest(
        key=str(uuid4()),
        reason="fixture",
        name="unsafe",
        adapter="openai_chat",
        base_url=url,
        api_key="secret",
    )
    with pytest.raises(AppException):
        m.service.save(m.actor, body, "req_test", "connection")
    assert not m.service.catalog()["resources"]


def test_test_required_publish_full_snapshot_and_stale_quote_rejection(models):
    m = models
    connection = m.connection()
    model = m.model(connection["revision_id"])
    with pytest.raises(AppException) as error:
        m.publish(model["revision_id"])
    assert error.value.code == "MODEL_TEST_REQUIRED"
    m.test(model["revision_id"], phase="connection")
    with pytest.raises(AppException):
        m.publish(model["revision_id"])
    m.test(model["revision_id"])
    first = m.publish(model["revision_id"])
    with m.d.sessions() as session:
        snapshot = runtime.capture(session)
    assert (
        runtime.llm("question", snapshot, m.d.sessions).model
        == "cfg_" + UUID(model["revision_id"]).hex
    )
    new = m.service.save(
        m.actor,
        ModelRequest(
            id=UUID(model["id"]),
            expected_version=1,
            key="edit-model",
            reason="fixture",
            name="new model",
            connection_revision=UUID(connection["revision_id"]),
            model_id="upstream-v2",
        ),
        "req_test",
        "model",
    )
    m.test(new["revision_id"])
    m.publish(new["revision_id"], version=1)
    assert (
        runtime.configuration(snapshot["bindings"]["summary"], m.d.sessions)[0][
            "model_id"
        ]
        == "upstream-v1"
    )
    with m.d.sessions() as session:
        with pytest.raises(AppException) as error:
            runtime.validate_snapshot(session, {"model_config": snapshot})
        assert error.value.code == "MODEL_CONFIGURATION_CHANGED"
    with pytest.raises(AppException):
        m.publish(model["revision_id"], version=0)
    rolled = m.service.publish(
        m.actor,
        PublishRequest(
            key="rollback",
            reason="fixture",
            expected_version=2,
            release_id=UUID(first["release_id"]),
        ),
        "req_test",
    )
    assert rolled["version"] == 3


async def test_runtime_uses_pinned_endpoint_key_and_upstream_model(models, monkeypatch):
    m = models
    c = m.connection()
    first = m.model(c["revision_id"])
    m.test(first["revision_id"])
    m.publish(first["revision_id"])
    with m.d.sessions() as session:
        snapshot = runtime.capture(session)
    captured = []

    class Fake:
        def __init__(self, url, key, model, **kwargs):
            captured.append((url, key, model))

        async def generate(self, request):
            return LLMResult({"ok": True}, 1, 1, 1, "upstream-v1")

    monkeypatch.setattr(
        "app.modules.ai.providers.openai_compatible.OpenAICompatibleLLMProvider", Fake
    )
    provider = runtime.llm("summary", snapshot, m.d.sessions)
    await provider.generate(LLMRequest("summary", {}, {}, "zh", "subtitle"))
    assert captured == [
        ("https://api.example.com/v1", "vendor-secret-test", "upstream-v1")
    ]
    with pytest.raises(AppException):
        provider.model = "different"
    credential = m.service.catalog()["resources"][0]["revisions"][0].get(
        "credential_id"
    )
    if credential is None:
        credential = next(
            r for r in m.service.catalog()["resources"] if r["kind"] == "connection"
        )["revisions"][0]["credential_id"]
    m.service.revoke(
        m.actor, UUID(credential), Operation(key="revoke", reason="fixture"), "req_test"
    )
    with pytest.raises(AppException):
        await provider.generate(LLMRequest("summary", {}, {}, "zh", "subtitle"))
    assert len(captured) == 1
    with pytest.raises(AppException):
        m.service.publish(
            m.actor,
            PublishRequest(
                key="rollback-revoked",
                reason="fixture",
                expected_version=1,
                release_id=UUID(snapshot["release_id"]),
            ),
            "req_test",
        )


def test_qwen_cannot_be_assigned_precise_timing_and_active_cannot_archive(models):
    m = models
    c = m.connection()
    text = m.model(c["revision_id"])
    m.test(text["revision_id"])
    m.publish(text["revision_id"])
    with pytest.raises(AppException) as error:
        m.service.archive(
            m.actor,
            UUID(c["id"]),
            ArchiveRequest(key="archive-active", reason="fixture", expected_version=1),
            "req_test",
        )
    assert error.value.code == "MODEL_IN_USE"
    q = m.connection(name="qwen", adapter="qwen_asr")
    asr = m.model(q["revision_id"])
    m.test(asr["revision_id"], capabilities=["transcription"])
    bindings = {
        use: text["revision_id"]
        if use not in {"transcription", "clip_timing"}
        else asr["revision_id"]
        for use in runtime.USES
    }
    with pytest.raises(AppException) as error:
        m.service.publish(
            m.actor,
            PublishRequest(
                key="bad-capability",
                reason="fixture",
                expected_version=1,
                bindings=bindings,
            ),
            "req_test",
        )
    assert error.value.code == "MODEL_TIMESTAMPS_REQUIRED"


async def test_probe_uses_task_service_no_quota_and_cancellation(models):
    m = models
    c = m.connection()
    model = m.model(c["revision_id"])
    body = ProbeRequest(
        key="probe",
        reason="fixture",
        revision_id=UUID(model["revision_id"]),
        phase="compatibility",
    )
    receipt = m.service.probe(m.actor, body, "req_test")
    task = m.d.tasks.create(
        "model_test",
        input_payload={
            "probe_id": receipt["probe_id"],
            "admin_version": m.actor.auth_version,
        },
        user_id=m.actor.id,
    )
    assert (
        m.d.tasks.create(
            "model_test",
            input_payload={
                "probe_id": receipt["probe_id"],
                "admin_version": m.actor.auth_version,
            },
            user_id=m.actor.id,
        ).id
        == task.id
    )

    async def checks(*args):
        return {"capabilities": ["summary"], "checks": ["fixture"]}

    result = await execute_test(
        task.id, TaskService(m.d.tasks, MemoryBus()), m.service, checks
    )
    assert result["status"] == "completed"
    assert m.service.catalog()["probes"][0]["result"]["capabilities"] == ["summary"]
    next_receipt = m.service.probe(
        m.actor, body.model_copy(update={"key": "cancel-probe"}), "req_test"
    )
    other = m.d.tasks.create(
        "model_test",
        input_payload={
            "probe_id": next_receipt["probe_id"],
            "admin_version": m.actor.auth_version,
        },
        user_id=m.actor.id,
    )
    entered = asyncio.Event()

    async def slow(*args):
        entered.set()
        await asyncio.sleep(30)

    running = asyncio.create_task(
        execute_test(other.id, TaskService(m.d.tasks, MemoryBus()), m.service, slow)
    )
    await asyncio.wait_for(entered.wait(), 2)
    m.d.tasks.request_cancel(other.id)
    await asyncio.wait_for(running, 3)
    assert m.d.tasks.get(other.id).status == "cancelled"


async def test_http_permissions_secret_masking_and_invalid_bindings(models):
    m = models
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(m.d.app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/admin/models")).status_code == 401
        client.cookies.set(
            "access_token",
            __import__("app.modules.auth.tokens", fromlist=["issue_token"]).issue_token(
                m.actor.id
            ),
        )
        body = {
            "key": "http-connection",
            "reason": "fixture",
            "name": "HTTP",
            "adapter": "openai_chat",
            "base_url": "https://api.example.com/v1",
            "api_key": "http-secret",
        }
        assert (
            await client.post("/api/v1/admin/models/connections", json=body)
        ).status_code == 403
        response = await client.post(
            "/api/v1/admin/models/connections", json=body, headers=headers(m.d)
        )
        assert response.status_code == 200
        catalog = (await client.get("/api/v1/admin/models", headers=headers(m.d))).text
        assert "http-secret" not in catalog and '"has_key":true' in catalog
        assert (
            await client.post(
                "/api/v1/admin/models/publish",
                json={
                    "key": "bad-release",
                    "reason": "fixture",
                    "expected_version": 0,
                    "bindings": {"summary": "not-a-uuid"},
                },
                headers=headers(m.d),
            )
        ).status_code == 422


@pytest.mark.parametrize("reason_fields", [{}, {"reason": ""}, {"reason": "   "}])
async def test_optional_reasons_save_and_publish_through_http(models, monkeypatch, reason_fields):
    m = models
    monkeypatch.setattr(settings, "CLIPS_ENABLED", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(m.d.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/admin/models/connections",
            headers=headers(m.d),
            json={
                "key": "optional-connection",
                "name": "Optional reason connection",
                "adapter": "openai_chat",
                "base_url": "https://api.example.com/v1",
                "api_key": "fixture-secret",
                **reason_fields,
            },
        )
        assert response.status_code == 200, response.text
        connection_id = response.json()["data"]["revision_id"]
        response = await client.post(
            "/api/v1/admin/models/models",
            headers=headers(m.d),
            json={
                "key": "optional-model",
                "name": "Optional reason model",
                "connection_revision": connection_id,
                "model_id": "fixture-model",
                **reason_fields,
            },
        )
        assert response.status_code == 200, response.text
        model_id = response.json()["data"]["revision_id"]
        m.test(model_id)
        response = await client.post(
            "/api/v1/admin/models/publish",
            headers=headers(m.d),
            json={
                "key": "optional-publish",
                "expected_version": 0,
                "bindings": {
                    use: None if use in {"transcription", "clip_timing"} else model_id
                    for use in runtime.USES
                },
                **reason_fields,
            },
        )
        assert response.status_code == 200, response.text
        assert m.service.catalog()["releases"][0]["reason"] == "未填写"
        retry = await client.post(
            "/api/v1/admin/models/publish",
            headers=headers(m.d),
            json={
                "key": "optional-publish",
                "expected_version": 0,
                "bindings": {
                    use: None if use in {"transcription", "clip_timing"} else model_id
                    for use in runtime.USES
                },
                **reason_fields,
            },
        )
        assert retry.status_code == 200
        assert retry.json()["data"]["duplicate"] is True


async def test_business_probe_runs_real_validators_and_reports_capabilities(
    models, monkeypatch
):
    m = models
    c = m.connection()
    model = m.model(c["revision_id"])
    from app.modules.ai.providers.heuristic import HeuristicLLMProvider

    async def offline(self, request):
        return await HeuristicLLMProvider().generate(request)

    monkeypatch.setattr(runtime.ConfiguredLLM, "generate", offline)

    async def cancelled():
        return False

    async def progress(*args):
        pass

    result = await run_checks(
        model["revision_id"], "compatibility", m.d.sessions, cancelled, progress
    )
    assert set(result["capabilities"]) == {
        "summary",
        "mindmap",
        "question",
        "clip_analysis",
    }


async def test_mindmap_routes_summary_and_tree_to_separate_pinned_models(
    models, monkeypatch
):
    m = models
    c = m.connection()
    summary = m.model(c["revision_id"])
    mindmap = m.model(c["revision_id"])
    for item in (summary, mindmap):
        m.test(item["revision_id"])
    bindings = {
        use: summary["revision_id"]
        if use not in {"transcription", "clip_timing"}
        else None
        for use in runtime.USES
    }
    bindings["mindmap"] = mindmap["revision_id"]
    m.service.publish(
        m.actor,
        PublishRequest(
            key="split", reason="fixture", expected_version=0, bindings=bindings
        ),
        "req_test",
    )
    with m.d.sessions() as session:
        snapshot = runtime.capture(session)
    calls = []

    async def generate(self, request):
        calls.append((self.identifier, request.purpose))
        return LLMResult({}, 1, 1, 1, "fixture")

    monkeypatch.setattr(runtime.ConfiguredLLM, "generate", generate)
    provider = runtime.llm("mindmap", snapshot, m.d.sessions)
    await provider.generate(LLMRequest("summary", {}, {}, "zh", "subtitle"))
    await provider.generate(LLMRequest("mindmap", {}, {}, "zh", "subtitle"))
    assert calls == [
        (summary["revision_id"], "summary"),
        (mindmap["revision_id"], "mindmap"),
    ]
    assert provider.summary_model != provider.model


def test_task_creation_revalidates_unbound_quote_but_retries_existing_task(models):
    m = models
    quotas = AllowanceRepository(m.d.sessions)
    payload = {
        "source_kind": "transcript",
        "transcript_id": str(uuid4()),
        "title": "fixture",
        "duration_seconds": 0,
        "output_language": "zh",
    }
    old_quote = quotas.save_quote(m.d.target.id, "ai_summary", payload)
    c = m.connection()
    model = m.model(c["revision_id"])
    m.test(model["revision_id"])
    m.publish(model["revision_id"])
    _, old_payload, _ = quotas.quote(m.d.target.id, UUID(old_quote["quote_id"]))
    with pytest.raises(AppException) as error:
        m.d.tasks.create("ai_summary", input_payload=old_payload, user_id=m.d.target.id)
    assert error.value.code == "MODEL_CONFIGURATION_CHANGED"
    quote = quotas.save_quote(m.d.target.id, "ai_summary", payload)
    _, confirmed, _ = quotas.quote(m.d.target.id, UUID(quote["quote_id"]))
    task = m.d.tasks.create(
        "ai_summary", input_payload=confirmed, user_id=m.d.target.id
    )
    m.publish(model["revision_id"], version=1)
    assert (
        m.d.tasks.create(
            "ai_summary", input_payload=confirmed, user_id=m.d.target.id
        ).id
        == task.id
    )


async def test_probe_http_uses_queue_and_demoted_actor_cannot_start(
    models, monkeypatch
):
    m = models
    c = m.connection()
    model = m.model(c["revision_id"])
    dispatch = []
    monkeypatch.setattr(
        "app.modules.model_config.worker.ModelTestDispatcher.enqueue",
        lambda self, task_id: dispatch.append(task_id),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(m.d.app),
        base_url="http://test",
        headers=headers(m.d),
    ) as client:
        response = await client.post(
            "/api/v1/admin/models/tests",
            json={
                "key": "queued-probe",
                "reason": "fixture",
                "revision_id": model["revision_id"],
                "phase": "connection",
            },
        )
        assert response.status_code == 200
        identifier = UUID(response.json()["data"]["task_id"])
        assert dispatch == [identifier]
    with m.d.sessions.begin() as session:
        session.get(User, m.actor.id).role = "user"
    checks = AsyncMock()
    await execute_test(
        identifier, TaskService(m.d.tasks, MemoryBus()), m.service, checks
    )
    assert m.d.tasks.get(identifier).error_code == "MODEL_TEST_AUTH_REVOKED"
    checks.assert_not_awaited()


async def test_asr_probe_checks_real_segments_and_qwen_cannot_claim_precise_timing(
    models, monkeypatch
):
    from app.modules.transcript.data import make_transcript

    m = models

    async def sample(self, path, language):
        return make_transcript(
            "en", [{"start": 0.0, "end": 2.0, "text": "The meeting starts at nine."}]
        )

    monkeypatch.setattr(
        "app.modules.transcript.providers.OpenAICompatibleASRProvider.transcribe",
        sample,
    )
    monkeypatch.setattr(
        "app.modules.transcript.providers.QwenASRProvider.transcribe", sample
    )

    async def cancelled():
        return False

    async def progress(*args):
        pass

    c = m.connection(adapter="openai_asr")
    model = m.model(c["revision_id"])
    marker = runtime.probe_context.set(True)
    try:
        result = await run_checks(
            model["revision_id"], "compatibility", m.d.sessions, cancelled, progress
        )
    finally:
        runtime.probe_context.reset(marker)
    assert result["capabilities"] == ["transcription", "clip_timing"]
    q = m.connection(name="Qwen", adapter="qwen_asr")
    model = m.model(q["revision_id"])
    result = await run_checks(
        model["revision_id"], "compatibility", m.d.sessions, cancelled, progress
    )
    assert result["capabilities"] == ["transcription"]
    health = m.service.catalog()["health"]
    assert any(
        row["scope"] == "test" and row["count"] == 1 and row["success_count"] == 1
        for row in health
    )


async def test_filetrans_probe_publish_and_frozen_runtime_binding(models, monkeypatch):
    from app.modules.transcript.data import make_transcript

    m = models
    monkeypatch.setattr(settings, "CLIPS_ENABLED", True)
    calls = []

    async def sample(self, path, language):
        calls.append((self.model, self.timeout, self.max_bytes))
        return make_transcript(
            "en", [{"start": 0.25, "end": 1.5, "text": "The meeting starts at nine."}]
        )

    monkeypatch.setattr(
        "app.modules.transcript.qwen_filetrans.QwenFiletransASRProvider.transcribe",
        sample,
    )
    connection = m.connection(adapter="qwen_filetrans")
    model = m.model(connection["revision_id"], timeout=180, max_upload_mb=15)

    async def cancelled():
        return False

    async def progress(*args):
        pass

    result = await run_checks(
        model["revision_id"], "compatibility", m.d.sessions, cancelled, progress
    )
    assert result["capabilities"] == ["transcription", "clip_timing"]
    m.test(model["revision_id"], capabilities=result["capabilities"])
    text_connection = m.connection()
    text_model = m.model(text_connection["revision_id"])
    m.test(text_model["revision_id"])
    bindings = {
        use: model["revision_id"]
        if use in {"transcription", "clip_timing"}
        else text_model["revision_id"]
        for use in runtime.USES
    }
    m.service.publish(
        m.actor,
        PublishRequest(key="publish-filetrans", expected_version=0, bindings=bindings),
        "req_test",
    )
    with m.d.sessions() as session:
        snapshot = runtime.capture(session)
    provider = runtime.asr("clip_timing", snapshot, m.d.sessions)
    assert provider.precise_timestamps and provider.available
    # Editing a model must not redirect an already-created task to a new revision.
    m.model(
        connection["revision_id"], id=UUID(model["id"]), expected_version=1, timeout=300
    )
    reply = await provider.transcribe(Path("not-read-by-fixture.wav"), "en")
    assert reply.segments[0]["start"] == 0.25
    assert calls[-1] == ("upstream-v1", 180, 15 * 1024 * 1024)
    assert (
        m.service.catalog()["adapters"]["qwen_filetrans"]["supports_timestamps"] is True
    )


async def test_filetrans_probe_rejects_timestamps_outside_the_fixed_sample(
    models, monkeypatch
):
    from app.modules.transcript.data import make_transcript

    m = models

    async def invalid(self, path, language):
        return make_transcript(
            "en", [{"start": 0, "end": 999, "text": "The meeting starts at nine."}]
        )

    monkeypatch.setattr(
        "app.modules.transcript.qwen_filetrans.QwenFiletransASRProvider.transcribe",
        invalid,
    )
    connection = m.connection(adapter="qwen_filetrans")
    model = m.model(connection["revision_id"])
    with pytest.raises(AppException) as error:
        await run_checks(
            model["revision_id"],
            "compatibility",
            m.d.sessions,
            lambda: False,
            lambda *args: None,
        )
    assert error.value.code == "MODEL_TIMESTAMPS_INVALID"


def test_audit_failure_rolls_back_encrypted_connection_and_secret(models):
    m = models

    class BrokenAudit(AuditRepository):
        def record_action(self, *args, **kwargs):
            super().record_action(*args, **kwargs)
            raise RuntimeError("audit failure")

    service = ModelConfigService(m.d.sessions)
    original = service.mutate

    def broken(actor, action, target, body, request_id, operation, data=None):
        return AdminService(m.d.sessions, BrokenAudit()).model_operation(
            actor,
            "model." + action,
            target,
            body.reason,
            body.key,
            request_id,
            data or {},
            operation,
        )

    service.mutate = broken
    body = ConnectionRequest(
        key="rollback",
        reason="fixture",
        name="rollback",
        adapter="openai_chat",
        base_url="https://api.example.com/v1",
        api_key="fake-key",
    )
    with pytest.raises(RuntimeError):
        service.save(m.actor, body, "req_test", "connection")
    with m.d.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ModelCredential)) == 0
        assert session.scalar(select(func.count()).select_from(ModelResource)) == 0


def test_disabled_clip_features_do_not_block_text_only_publication(models, monkeypatch):
    m = models
    monkeypatch.setattr(settings, "CLIPS_ENABLED", False)
    c = m.connection()
    model = m.model(c["revision_id"])
    m.test(model["revision_id"], capabilities=["summary", "mindmap", "question"])
    bindings = {
        use: model["revision_id"] if use in {"summary", "mindmap", "question"} else None
        for use in runtime.USES
    }
    result = m.service.publish(
        m.actor,
        PublishRequest(
            key="text-only", reason="fixture", expected_version=0, bindings=bindings
        ),
        "req_test",
    )
    assert result["version"] == 1
    assert set(m.service.catalog()["enabled_uses"]) == {
        "summary",
        "mindmap",
        "question",
        "transcription",
    }


async def test_asr_worker_uses_owned_storage_and_cleans_temp_after_probe(
    models, monkeypatch, tmp_path
):
    from app.modules.transcript.providers import asr_work_directory

    m = models
    monkeypatch.setattr(settings, "STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "STORAGE_MIN_FREE_MB", 0)
    connection = m.connection(adapter="qwen_asr")
    model = m.model(connection["revision_id"])
    receipt = m.service.probe(
        m.actor,
        ProbeRequest(
            key="asr-storage",
            reason="fixture",
            revision_id=UUID(model["revision_id"]),
            phase="compatibility",
        ),
        "req_test",
    )
    task = m.d.tasks.create(
        "model_test",
        input_payload={
            "probe_id": receipt["probe_id"],
            "admin_version": m.actor.auth_version,
        },
        user_id=m.actor.id,
    )

    async def checks(*args):
        directory = asr_work_directory.get()
        assert (
            directory == settings.storage_path / "tmp" / str(task.id)
            and directory.is_dir()
        )
        (directory / "test.txt").write_text("temporary probe data")
        return {"capabilities": ["transcription"]}

    await execute_test(task.id, TaskService(m.d.tasks, MemoryBus()), m.service, checks)
    assert m.d.tasks.get(task.id).status == "completed"
    assert not (settings.storage_path / "tmp" / str(task.id)).exists()
    assert not list((settings.storage_path / ".reservations").glob("*.json"))


def test_catalog_retains_each_revisions_latest_proof_beyond_global_history(models):
    m = models
    c = m.connection()
    verified = m.model(c["revision_id"])
    m.test(verified["revision_id"])
    other = m.model(c["revision_id"])
    from app.modules.billing.policy import now

    with m.d.sessions.begin() as session:
        for index in range(105):
            session.add(
                ModelProbe(
                    id=uuid4(),
                    revision_id=UUID(other["revision_id"]),
                    phase="connection",
                    user_id=m.actor.id,
                    created_at=now() + timedelta(seconds=index + 1),
                )
            )
    catalog = m.service.catalog()
    proof = next(
        p for p in catalog["probes"] if p["revision_id"] == verified["revision_id"]
    )
    assert proof["status"] == "completed"
    assert "summary" in proof["result"]["capabilities"]
    assert len(catalog["probes"]) == 2
    m.publish(verified["revision_id"])


def test_probe_duplicate_reservations_and_global_queue_bound(models):
    m = models
    c = m.connection()
    versions = [m.model(c["revision_id"]) for _ in range(4)]

    def request(index, phase="compatibility"):
        return ProbeRequest(
            key=str(uuid4()),
            reason="test queue",
            revision_id=UUID(versions[index]["revision_id"]),
            phase=phase,
        )

    body = request(0)
    first = m.service.probe(m.actor, body, "req_test")
    assert m.service.probe(m.actor, body, "req_test")["probe_id"] == first["probe_id"]
    with pytest.raises(AppException) as error:
        m.service.probe(m.actor, request(0), "req_test")
    assert error.value.code == "MODEL_TEST_IN_PROGRESS"
    # A bound queued task and an unbound acceptance both consume queue capacity.
    task = m.d.tasks.create(
        "model_test",
        input_payload={
            "probe_id": first["probe_id"],
            "admin_version": m.actor.auth_version,
        },
        user_id=m.actor.id,
    )
    m.service.probe(m.actor, request(1), "req_test")
    m.service.probe(m.actor, request(2), "req_test")
    with pytest.raises(AppException) as error:
        m.service.probe(m.actor, request(3), "req_test")
    assert error.value.code == "MODEL_TEST_QUEUE_FULL"
    m.d.tasks.start(task.id)
    m.d.tasks.complete_with(
        task.id, {"capabilities": ["summary"]}, lambda session: None
    )
    m.service.probe(m.actor, request(3), "req_test")


def test_shared_credential_impact_and_publication_audit_metadata(models):
    m = models
    c = m.connection()
    old_model = m.model(c["revision_id"])
    m.test(old_model["revision_id"])
    publication = m.publish(old_model["revision_id"])
    edited = m.service.save(
        m.actor,
        ConnectionRequest(
            id=UUID(c["id"]),
            expected_version=1,
            key="shared-key-edit",
            reason="rename",
            name="renamed service",
            adapter="openai_chat",
            base_url="https://api.example.com/v1",
        ),
        "req_test",
        "connection",
    )
    new_model = m.model(edited["revision_id"])
    credential = next(
        r for r in m.service.catalog()["resources"] if r["kind"] == "connection"
    )["revisions"][0]["credential_id"]
    impact = m.service.credential_impact(UUID(credential))
    assert impact["version"] == 1
    assert len(impact["connections"]) == 2
    assert {item["id"] for item in impact["models"]} == {
        old_model["revision_id"],
        new_model["revision_id"],
    }
    active = next(
        item for item in impact["models"] if item["id"] == old_model["revision_id"]
    )
    assert "summary" in active["active_uses"]
    release = m.service.catalog()["releases"][0]
    assert release["id"] == publication["release_id"]
    assert release["reason"] == "publish"
    assert release["actor_id"] == str(m.actor.id)
    assert release["actor_label"] == m.actor.nickname
    assert release["version"] == 1
    from app.modules.model_config.router import RevokeRequest

    with pytest.raises(AppException) as error:
        m.service.revoke(
            m.actor,
            UUID(credential),
            RevokeRequest(key="stale-revoke", reason="test", expected_version=0),
            "req_test",
        )
    assert error.value.code == "MODEL_CONFIG_CONFLICT"
    m.service.revoke(
        m.actor,
        UUID(credential),
        RevokeRequest(key="revoke-shared", reason="test", expected_version=1),
        "req_test",
    )
    resource = next(
        r for r in m.service.catalog()["resources"] if r["kind"] == "connection"
    )
    assert all(v["revoked"] for v in resource["revisions"])


def test_stale_unbound_probe_does_not_poll_forever_or_bypass_queue_limit(models):
    m = models
    c = m.connection()
    model = m.model(c["revision_id"])
    body = ProbeRequest(
        key="unbound-probe",
        reason="fixture",
        revision_id=UUID(model["revision_id"]),
        phase="connection",
    )
    first = m.service.probe(m.actor, body, "req_test")
    from app.modules.billing.policy import now

    with m.d.sessions.begin() as session:
        probe = session.get(ModelProbe, UUID(first["probe_id"]))
        probe.created_at = now() - timedelta(minutes=3)
    assert m.service.catalog()["probes"][0]["status"] == "failed"
    with pytest.raises(AppException) as error:
        m.d.tasks.create(
            "model_test",
            input_payload={
                "probe_id": first["probe_id"],
                "admin_version": m.actor.auth_version,
            },
            user_id=m.actor.id,
        )
    assert error.value.code == "MODEL_TEST_EXPIRED"
    m.service.probe(m.actor, body.model_copy(update={"key": "fresh-probe"}), "req_test")


def test_catalog_keeps_exact_connection_for_unpublished_model_after_many_edits(models):
    m = models
    c = m.connection()
    model = m.model(c["revision_id"])
    with m.d.sessions.begin() as session:
        old = session.get(ModelRevision, UUID(c["revision_id"]))
        for version in range(2, 62):
            session.add(
                ModelRevision(
                    id=uuid4(),
                    resource_id=old.resource_id,
                    version=version,
                    payload=dict(old.payload),
                )
            )
        session.get(ModelResource, old.resource_id).version = 61
    catalog = m.service.catalog()
    connection = next(r for r in catalog["resources"] if r["kind"] == "connection")
    assert connection["revisions"][0]["version"] == 61
    assert any(
        v["id"] == c["revision_id"] and v["has_key"] for v in connection["revisions"]
    )
    m.test(model["revision_id"])
    m.publish(model["revision_id"])
