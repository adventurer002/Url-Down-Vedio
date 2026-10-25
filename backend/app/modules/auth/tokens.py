from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.core.config import settings
from app.core.exceptions import AppException


def signing_key():
    if settings.APP_ENV != "development" and settings.JWT_SECRET_KEY == "dev-secret-change-in-production":
        raise AppException("AUTH_MISCONFIGURED", "生产环境必须配置独立 JWT 密钥", 503)
    return settings.JWT_SECRET_KEY


def issue_token(user_id):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "iat": now,
                       "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
                       "iss": "videohub", "aud": "videohub-web"}, signing_key(), algorithm="HS256")


def decode_token(token):
    try:
        claims = jwt.decode(token, signing_key(), algorithms=["HS256"],
                            issuer="videohub", audience="videohub-web",
                            options={"require": ["exp", "iat", "sub"]})
        return UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise AppException("INVALID_TOKEN", "登录凭证无效或已过期，请重新登录", 401) from None
