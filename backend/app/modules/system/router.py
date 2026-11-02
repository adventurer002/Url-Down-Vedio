from fastapi import APIRouter, Depends, Request

from app.core.response import success_response
from app.modules.auth.dependencies import admin_writer
from app.modules.admin.router import OperationRequest, get_admin_service
from app.modules.system.service import CleanupService, get_cleanup_service

router = APIRouter(prefix="/system", tags=["system"])


@router.post("/cleanup")
async def cleanup(request: Request, body: OperationRequest | None = None,
                   actor=Depends(admin_writer), service=Depends(get_cleanup_service), admin=Depends(get_admin_service)):
    # Preserve the existing settings button while routing every cleanup through
    # audit. Legacy requests use their server request ID as a one-shot key.
    reason = body.reason if body else "设置页清理过期文件"
    key = body.key if body else request.state.request_id
    return success_response(await admin.cleanup(actor, reason, key, request.state.request_id, service))
