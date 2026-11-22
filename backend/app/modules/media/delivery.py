"""Authorize private object downloads through a gateway or a direct storage link."""
from datetime import timedelta, timezone
from hmac import compare_digest
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import RedirectResponse
import jwt
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.infrastructure.storage.objects import SIGNED_URL_SECONDS, object_key, object_store, store_tag, trusted_origin
from app.modules.billing.repository import AllowanceRepository
from app.modules.billing.transfer import transfer_range
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import database_call

router = APIRouter(prefix="/internal/object-delivery", include_in_schema=False)
TICKET_SECONDS = 60
TICKET_AUDIENCE = "videohub-object-delivery"
REDIRECT_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transfer_id: UUID
    storage_key: str
    tag: str
    size: int = Field(gt=0)
    bounds: tuple[int, int] | None = None
    filename: str
    mime_type: str

    @property
    def length(self):
        return self.bounds[1] - self.bounds[0] + 1 if self.bounds else self.size

    @property
    def byte_range(self):
        return f"bytes={self.bounds[0]}-{self.bounds[1]}" if self.bounds else None


def delivery_secret():
    secret = settings.OBJECT_DELIVERY_SECRET.get_secret_value()
    if len(secret) < 32 or not secret.isascii() or any(not 33 <= ord(char) <= 126 for char in secret):
        raise AppException("OBJECT_CONFIG_INVALID", "未配置对象分发认证密钥", 503)
    return secret


def decode_ticket(token):
    try:
        claims = jwt.decode(token, delivery_secret(), algorithms=["HS256"], audience=TICKET_AUDIENCE,
                            options={"require": ["exp", "aud", "download"]})
        delivery = Delivery.model_validate(claims["download"])
        if delivery.bounds and not 0 <= delivery.bounds[0] <= delivery.bounds[1] < delivery.size:
            raise ValueError
        if delivery.mime_type not in {"video/mp4", "audio/mpeg"} or delivery.tag != store_tag():
            raise ValueError
        return delivery
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise AppException("OBJECT_TICKET_INVALID", "对象下载凭证无效或已过期", 403) from None


def _file_expiry(record):
    expiry = record.expires_at
    return expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=timezone.utc)


def delivery_destination(record, filename):
    if not settings.OBJECT_DELIVERY_ENABLED or not settings.ALLOWANCES_ENABLED or not record.object_store_tag:
        return None
    try:
        if record.object_store_tag != store_tag():
            return None
        key = object_key(record.object_store_tag, record.storage_key)
        if settings.OBJECT_DELIVERY_MODE == "direct":
            expires_in = min(SIGNED_URL_SECONDS, int((_file_expiry(record) - utcnow()).total_seconds()))
            if expires_in < 2:
                return None
            return object_store().sign_download(key, filename, expires_in)
        delivery_secret()
        return trusted_origin(settings.OBJECT_GATEWAY_URL)
    except AppException:
        return None


async def redirect_download(record, filename, request, user, repository):
    if (not user or request.query_params.get("delivery") == "local"
            or request.method != "GET"):
        return None
    direct = settings.OBJECT_DELIVERY_MODE == "direct"
    if not direct and "if-range" in request.headers:
        return None
    destination = delivery_destination(record, filename)
    if not destination:
        return None
    bounds = transfer_range(record.size_bytes, request.headers.get("range"))
    if direct:
        await database_call(repository.charge_object_download, user.id, record.id, record.size_bytes)
        return RedirectResponse(destination, status_code=307, headers=REDIRECT_HEADERS)
    length = bounds[1] - bounds[0] + 1 if bounds else record.size_bytes
    expiry = min(_file_expiry(record), utcnow() + timedelta(seconds=TICKET_SECONDS))
    if (expiry - utcnow()).total_seconds() < 2:
        return None
    identifier = await database_call(repository.reserve_transfer, user.id, length, claim_expires_at=expiry)
    delivery = Delivery(transfer_id=identifier, storage_key=record.storage_key, tag=record.object_store_tag,
                        size=record.size_bytes, bounds=bounds, filename=filename, mime_type=record.mime_type)
    token = jwt.encode({"aud": TICKET_AUDIENCE, "exp": expiry, "download": delivery.model_dump(mode="json")},
                       delivery_secret(), algorithm="HS256")
    return RedirectResponse(destination + "/download?" + urlencode({"token": token}), status_code=307,
                            headers=REDIRECT_HEADERS)


class TransferReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bytes: int = Field(ge=0, le=5 * 1024**3)


def authorize_gateway(key):
    try:
        configured = delivery_secret()
    except AppException:
        configured = ""
    if not configured or not key or not compare_digest(key.encode(), configured.encode()):
        raise AppException("OBJECT_GATEWAY_FORBIDDEN", "对象分发回调未授权", 403)


@router.post("/{identifier}/claim")
async def claim(identifier: UUID, report: TransferReport, x_object_gateway_key: str = Header(default="")):
    authorize_gateway(x_object_gateway_key)
    accepted = await database_call(AllowanceRepository(SessionLocal).claim_delivery, identifier, report.bytes)
    if not accepted:
        raise AppException("OBJECT_TICKET_USED", "对象下载凭证已使用或过期", 409)
    return {"data": {"accepted": True}}


@router.post("/{identifier}/settle")
async def settle(identifier: UUID, report: TransferReport, x_object_gateway_key: str = Header(default="")):
    authorize_gateway(x_object_gateway_key)
    accepted = await database_call(AllowanceRepository(SessionLocal).settle_delivery, identifier, report.bytes)
    if not accepted:
        raise AppException("OBJECT_TRANSFER_INVALID", "对象下载结算记录无效", 409)
    return {"data": {"accepted": True}}
