from decimal import Decimal
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from app.core.response import success_response
from app.modules.auth.dependencies import required_admin, admin_writer, task_user_id
from app.modules.tasks.service import TaskService, database_call
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.router import get_task_service
from app.infrastructure.database.session import SessionLocal
from app.modules.model_config.service import ModelConfigService
from app.modules.model_config.worker import ModelTestDispatcher

router = APIRouter(
    prefix="/admin/models",
    tags=["model management"],
    dependencies=[Depends(required_admin)],
)


def get_model_config_service():
    return ModelConfigService(SessionLocal)


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class RevisionRequest(Operation):
    id: UUID | None = None
    expected_version: int = Field(default=0, ge=0, strict=True)
    name: str = Field(min_length=1, max_length=80)


class ConnectionRequest(RevisionRequest):
    adapter: Literal["openai_chat", "openai_asr", "qwen_asr"]
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class ModelRequest(RevisionRequest):
    connection_revision: UUID
    model_id: str = Field(min_length=1, max_length=128)
    timeout: int = Field(default=90, ge=5, le=600, strict=True)
    max_tokens: int = Field(default=4096, ge=128, le=16384, strict=True)
    max_upload_mb: int = Field(default=25, ge=1, le=100, strict=True)
    input_price: Decimal | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )
    output_price: Decimal | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )


class ProbeRequest(Operation):
    revision_id: UUID
    phase: Literal["connection", "compatibility"]


class PublishRequest(Operation):
    expected_version: int = Field(ge=0, strict=True)
    bindings: dict[str, str | None] = Field(default_factory=dict, max_length=6)
    release_id: UUID | None = None


class ArchiveRequest(Operation):
    expected_version: int = Field(ge=0, strict=True)


class RevokeRequest(Operation):
    expected_version: int | None = Field(default=None, ge=0, strict=True)


@router.get("")
async def catalog(service=Depends(get_model_config_service)):
    return success_response(await database_call(service.catalog))


@router.get("/credentials/{identifier}/impact")
async def credential_impact(
    identifier: UUID, service=Depends(get_model_config_service)
):
    return success_response(await database_call(service.credential_impact, identifier))


@router.post("/connections")
async def save_connection(
    body: ConnectionRequest,
    request: Request,
    actor=Depends(admin_writer),
    service=Depends(get_model_config_service),
):
    return success_response(
        await database_call(
            service.save, actor, body, request.state.request_id, "connection"
        )
    )


@router.post("/models")
async def save_model(
    body: ModelRequest,
    request: Request,
    actor=Depends(admin_writer),
    service=Depends(get_model_config_service),
):
    return success_response(
        await database_call(
            service.save, actor, body, request.state.request_id, "model"
        )
    )


@router.post("/tests")
async def probe(
    body: ProbeRequest,
    request: Request,
    actor=Depends(admin_writer),
    service=Depends(get_model_config_service),
    tasks=Depends(get_task_service),
):
    result = await database_call(service.probe, actor, body, request.state.request_id)
    marker = task_user_id.set(actor.id)
    try:
        test_tasks = TaskService(
            TaskRepository(service.sessions), tasks.bus, ModelTestDispatcher()
        )
        task = await test_tasks.create(
            "model_test",
            input_payload={
                "probe_id": result["probe_id"],
                "admin_version": actor.auth_version,
            },
        )
        return success_response(
            {**result, "task_id": str(task.id), "status": task.status}
        )
    finally:
        task_user_id.reset(marker)


@router.post("/publish")
async def publish(
    body: PublishRequest,
    request: Request,
    actor=Depends(admin_writer),
    service=Depends(get_model_config_service),
):
    return success_response(
        await database_call(service.publish, actor, body, request.state.request_id)
    )


@router.post("/resources/{identifier}/archive")
async def archive(
    identifier: UUID,
    body: ArchiveRequest,
    request: Request,
    actor=Depends(admin_writer),
    service=Depends(get_model_config_service),
):
    return success_response(
        await database_call(
            service.archive, actor, identifier, body, request.state.request_id
        )
    )


@router.post("/credentials/{identifier}/revoke")
async def revoke(
    identifier: UUID,
    body: RevokeRequest,
    request: Request,
    actor=Depends(admin_writer),
    service=Depends(get_model_config_service),
):
    return success_response(
        await database_call(
            service.revoke, actor, identifier, body, request.state.request_id
        )
    )
