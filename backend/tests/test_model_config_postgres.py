from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select, func
from test_allowances_postgres import postgres
from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.auth.repository import UserRepository
from app.modules.admin.service import AdminService
from app.modules.model_config.models import ModelResource, ModelCredential, ModelRelease
from app.modules.model_config.service import ModelConfigService
from app.modules.model_config.router import (
    ConnectionRequest,
    ModelRequest,
    ProbeRequest,
    PublishRequest,
)
from app.modules.model_config.runtime import USES


def setup(postgres, monkeypatch):
    sessions, tasks, _, user, _ = postgres
    monkeypatch.setattr(settings, "MODEL_CONFIG_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "MODEL_CONFIG_ENCRYPTION_KEY",
        SecretStr(Fernet.generate_key().decode()),
    )
    AdminService(sessions).bootstrap(user.id, "operator", "fixture", "bootstrap")
    actor = UserRepository(sessions).get(user.id)
    return sessions, tasks, actor, ModelConfigService(sessions)


def test_postgres_duplicate_secret_save_is_one_credential_and_one_revision(
    postgres, monkeypatch
):
    sessions, _, actor, service = setup(postgres, monkeypatch)
    body = ConnectionRequest(
        key="same-connection",
        reason="fixture",
        name="same connection",
        adapter="openai_chat",
        base_url="https://api.example.com/v1",
        api_key="fake-key",
    )
    barrier = Barrier(3)

    def save(_):
        barrier.wait(timeout=5)
        return service.save(actor, body, "req_pg", "connection")

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(save, range(3)))
    assert len({result["revision_id"] for result in results}) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ModelCredential)) == 1
        assert session.scalar(select(func.count()).select_from(ModelResource)) == 1


def test_postgres_competing_publications_only_one_generation_wins(
    postgres, monkeypatch
):
    sessions, tasks, actor, service = setup(postgres, monkeypatch)
    c = service.save(
        actor,
        ConnectionRequest(
            key="connection",
            reason="fixture",
            name="connection",
            adapter="openai_chat",
            base_url="https://api.example.com/v1",
            api_key="fake-key",
        ),
        "req_pg",
        "connection",
    )
    m = service.save(
        actor,
        ModelRequest(
            key="model",
            reason="fixture",
            name="model",
            connection_revision=UUID(c["revision_id"]),
            model_id="upstream-model",
        ),
        "req_pg",
        "model",
    )
    p = service.probe(
        actor,
        ProbeRequest(
            key="probe",
            reason="fixture",
            revision_id=UUID(m["revision_id"]),
            phase="compatibility",
        ),
        "req_pg",
    )
    task = tasks.create(
        "model_test",
        input_payload={"probe_id": p["probe_id"], "admin_version": actor.auth_version},
        user_id=actor.id,
    )
    tasks.start(task.id)
    tasks.complete_with(
        task.id,
        {"capabilities": ["summary", "mindmap", "question", "clip_analysis"]},
        lambda session: None,
    )
    barrier = Barrier(2)
    bindings = {
        use: m["revision_id"] if use not in {"transcription", "clip_timing"} else None
        for use in USES
    }

    def publish(index):
        barrier.wait(timeout=5)
        try:
            return service.publish(
                actor,
                PublishRequest(
                    key=f"publish-{index}",
                    reason="fixture",
                    expected_version=0,
                    bindings=bindings,
                ),
                "req_pg",
            )
        except AppException as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(publish, range(2)))
    assert (
        sum(isinstance(result, dict) for result in results) == 1
        and "MODEL_CONFIG_CONFLICT" in results
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ModelRelease)) == 1


def test_postgres_competing_duplicate_tests_only_reserve_once(postgres, monkeypatch):
    sessions, _, actor, service = setup(postgres, monkeypatch)
    c = service.save(
        actor,
        ConnectionRequest(
            key="connection",
            reason="fixture",
            name="connection",
            adapter="openai_chat",
            base_url="https://api.example.com/v1",
            api_key="fake-key",
        ),
        "req_pg",
        "connection",
    )
    model = service.save(
        actor,
        ModelRequest(
            key="model",
            reason="fixture",
            name="model",
            connection_revision=UUID(c["revision_id"]),
            model_id="model",
        ),
        "req_pg",
        "model",
    )
    barrier = Barrier(3)

    def reserve(index):
        barrier.wait(timeout=5)
        try:
            return service.probe(
                actor,
                ProbeRequest(
                    key=f"probe-{index}",
                    reason="fixture",
                    revision_id=UUID(model["revision_id"]),
                    phase="compatibility",
                ),
                "req_pg",
            )
        except AppException as error:
            return error.code

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(reserve, range(3)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("MODEL_TEST_IN_PROGRESS") == 2
