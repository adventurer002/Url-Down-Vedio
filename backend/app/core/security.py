"""AuthN: bcrypt passwords, short-lived JWT access, single-use Redis refresh."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import bcrypt
import redis.asyncio as aredis
from jose import JWTError, jwt

from backend.app.core.config import settings
from backend.app.core.errors import AppError

ACCESS_TTL = timedelta(hours=2)
REFRESH_TTL = timedelta(days=14)


def _normalize(password: str) -> bytes:
    # bcrypt caps input at 72 bytes; pre-hash so long passphrases keep full entropy.
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    hashed: bytes = bcrypt.hashpw(_normalize(password), bcrypt.gensalt())
    return hashed.decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_normalize(password), password_hash.encode("ascii"))


def create_access_token(user_id: UUID, is_admin: bool = False) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "is_admin": is_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TTL).timestamp()),
    }
    token: str = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token


def decode_access_token(token: str) -> UUID:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        sub = payload.get("sub")
        if not sub:
            raise AppError("unauthorized", "登录已失效")
        return UUID(sub)
    except (JWTError, ValueError, AttributeError):
        raise AppError("unauthorized", "登录已失效")


def _refresh_key(token_hash: str) -> str:
    return f"refresh:{token_hash}"


def mint_refresh_token() -> tuple[str, str]:
    """Return (token, sha256) — only the hash is ever stored."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


async def store_refresh_token(token_hash: str, user_id: UUID) -> None:
    r = aredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        await r.set(_refresh_key(token_hash), str(user_id), ex=int(REFRESH_TTL.total_seconds()))
    finally:
        await r.aclose()


async def rotate_refresh_token(old_token: str) -> tuple[UUID, str] | None:
    """Single-use rotation. Returns (user_id, new_token) or None when invalid."""
    old_hash = hashlib.sha256(old_token.encode()).hexdigest()
    r = aredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        user_id = await r.getdel(_refresh_key(old_hash))
        if user_id is None:
            return None
        new_token, new_hash = mint_refresh_token()
        await r.set(
            _refresh_key(new_hash),
            user_id.decode() if isinstance(user_id, bytes) else user_id,
            ex=int(REFRESH_TTL.total_seconds()),
        )
        return UUID(user_id.decode() if isinstance(user_id, bytes) else user_id), new_token
    finally:
        await r.aclose()
