from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.core.config import settings
from app.core.exceptions import AppException


def signing_key():
    if settings.APP_ENV != "development" and settings.JWT_SECRET_KEY == "dev-secret-change-in-production":
        raise AppException("AUTH_MISCONFIGURED", "生产环境必须配置独立 JWT 密钥", 503)
    return settings.JWT_SECRET_KEY


def issue_token(user_id, auth_version=0):
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "ver": auth_version, "iat": now,
                       "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
                       "iss": "videohub", "aud": "videohub-web"}, signing_key(), algorithm="HS256")


def decode_identity(token):
    try:
        claims = jwt.decode(token, signing_key(), algorithms=["HS256"],
                            issuer="videohub", audience="videohub-web",
                            options={"require": ["exp", "iat", "sub"]})
        version = claims.get("ver", 0)  # Previously issued tokens remain valid only for unchanged accounts.
        if type(version) is not int or version < 0:
            raise ValueError("invalid credential version")
        return UUID(claims["sub"]), version
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise AppException("INVALID_TOKEN", "登录凭证无效或已过期，请重新登录", 401) from None


def decode_token(token):
    return decode_identity(token)[0]


RESOURCE_ACCESS_SECONDS = 120


def issue_resource_access(user_id, auth_version, purpose, resource_id):
    instant = datetime.now(timezone.utc)
    expires = instant + timedelta(seconds=RESOURCE_ACCESS_SECONDS)
    token = jwt.encode({"sub": str(user_id), "ver": auth_version, "iat": instant,
                        "exp": expires, "iss": "videohub", "aud": "videohub-resource",
                        "purpose": purpose, "resource": str(resource_id)}, signing_key(), algorithm="HS256")
    return {"access_token": token, "expires_at": expires.isoformat()}


def decode_resource_identity(token, purpose, resource_id):
    try:
        claims = jwt.decode(token, signing_key(), algorithms=["HS256"],
                            issuer="videohub", audience="videohub-resource",
                            options={"require": ["exp", "iat", "sub", "ver", "purpose", "resource"]})
        if (claims["purpose"] != purpose or claims["resource"] != str(resource_id)
                or type(claims["ver"]) is not int or claims["ver"] < 0):
            raise ValueError("invalid resource scope")
        return UUID(claims["sub"]), claims["ver"]
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise AppException("INVALID_RESOURCE_ACCESS", "访问链接已失效，请重新打开任务或下载文件", 401) from None
