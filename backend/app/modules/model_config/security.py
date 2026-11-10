from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import normalize_url


def cipher():
    try:
        primary = settings.MODEL_CONFIG_ENCRYPTION_KEY.get_secret_value()
        previous = settings.MODEL_CONFIG_PREVIOUS_KEYS.get_secret_value()
        if not primary:
            raise ValueError
        return MultiFernet(
            [
                Fernet(value.strip().encode())
                for value in [primary, *previous.split(",")]
                if value.strip()
            ]
        )
    except ValueError, TypeError:
        raise AppException(
            "MODEL_ENCRYPTION_NOT_CONFIGURED", "请先配置服务端模型密钥加密主密钥", 503
        ) from None


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        raise AppException(
            "MODEL_SECRET_UNAVAILABLE", "模型密钥无法解密，请检查服务端主密钥", 503
        ) from None


def endpoint(value):
    normalized, _, _ = normalize_url(value)
    parts = urlsplit(normalized)
    if (
        parts.scheme != "https"
        or parts.query
        or parts.fragment
        or urlsplit(value).fragment
    ):
        raise AppException(
            "MODEL_ENDPOINT_INVALID",
            "模型地址需使用公开 HTTPS，且不含查询参数或片段",
            422,
        )
    return normalized.rstrip("/")
