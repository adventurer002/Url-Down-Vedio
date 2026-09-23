"""WeChat Pay v3 Native (qr code_url) over raw HTTPS.

Auth: SHA256-RSA signature per request. Notify payloads are AES-256-GCM
encrypted with api_v3_key; callbacks verified by serial + signature.
"""

import base64
import json
import secrets
import time
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.app.providers.payment import (
    PaymentError,
    PayParams,
    WebhookEvent,
)

WXPAY_API = "https://api.mch.weixin.qq.com"


def load_private_key(pem: str):  # type: ignore[no-untyped-def]
    return serialization.load_pem_private_key(pem.encode(), password=None)


def load_cert_serial(pem: str) -> str:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(pem.encode())
    return f"{cert.serial_number:X}"


def build_authorization(
    mchid: str,
    serial_no: str,
    private_key: Any,
    method: str,
    url_path: str,
    body: str = "",
) -> str:
    nonce = secrets.token_hex(16)
    ts = str(int(time.time()))
    message = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body}\n"
    signature = private_key.sign(
        message.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    token = (
        f'mchid="{mchid}",nonce_str="{nonce}",timestamp="{ts}",'
        f'serial_no="{serial_no}",signature="{base64.b64encode(signature).decode()}"'
    )
    return f"WECHATPAY2-SHA256-RSA2048 {token}"


def decrypt_resource(api_v3_key: str, resource: dict[str, Any]) -> dict[str, Any]:
    try:
        aesgcm = AESGCM(api_v3_key.encode())
        plaintext = aesgcm.decrypt(
            base64.b64decode(str(resource["nonce"])),
            base64.b64decode(str(resource["ciphertext"])),
            str(resource.get("associated_data", "")).encode() or None,
        )
    except Exception as e:  # noqa: BLE001 - AES/GCM failures are heterogeneous
        raise PaymentError(f"微信回调解密失败: {e}")
    plain: dict[str, Any] = json.loads(plaintext.decode())
    return plain


class WechatPayProvider:
    """Native QR. Currency must be CNY; amounts passed in fen."""

    provider = "wechat"

    def __init__(
        self,
        mchid: str,
        appid: str,
        serial_no: str,
        private_key_pem: str,
        api_v3_key: str,
        notify_url: str,
    ) -> None:
        if not (mchid and serial_no and private_key_pem and api_v3_key):
            raise PaymentError("微信支付未配置", 500)
        self._mchid = mchid
        self._appid = appid
        self._serial_no = serial_no
        self._private_key = load_private_key(private_key_pem)
        self._api_v3_key = api_v3_key
        self._notify_url = notify_url

    async def create_payment(
        self, order_no: str, amount_cents: int, currency: str, subject: str
    ) -> PayParams:
        if currency.upper() != "CNY":
            raise PaymentError("微信支付仅支持人民币", 400)
        path = "/v3/pay/transactions/native"
        payload = {
            "appid": self._appid,
            "mchid": self._mchid,
            "description": subject[:127],
            "out_trade_no": order_no,
            "notify_url": self._notify_url,
            "amount": {"total": amount_cents, "currency": "CNY"},
        }
        body = json.dumps(payload, separators=(",", ":"))
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    WXPAY_API + path,
                    headers={
                        "Authorization": build_authorization(
                            self._mchid, self._serial_no, self._private_key, "POST", path, body
                        ),
                        "Content-Type": "application/json",
                    },
                    content=body,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise PaymentError(f"微信支付连接失败: {e}", 502)
        if resp.status_code != 200:
            raise PaymentError(f"微信下单失败 {resp.status_code}", 502)
        code_url = resp.json().get("code_url")
        if not code_url:
            raise PaymentError("微信未返回付款码", 502)
        return PayParams(provider="wechat", qr_code=str(code_url))

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        try:
            notify = json.loads(body.decode())
            resource = notify.get("resource") or {}
            plain = decrypt_resource(self._api_v3_key, resource)
        except PaymentError:
            raise
        except Exception as e:  # noqa: BLE001 - notify shapes vary
            raise PaymentError(f"微信回调格式错误: {e}")
        if plain.get("mchid") != self._mchid:
            raise PaymentError("微信回调商户号不符")
        state = str(plain.get("trade_state", ""))
        amount = plain.get("amount") or {}
        event = WebhookEvent(
            provider="wechat",
            provider_trade_no=str(plain.get("transaction_id") or ""),
            order_no=str(plain.get("out_trade_no") or ""),
            amount_cents=int(amount.get("total") or 0),
            status="success" if state == "SUCCESS" else "failed",
            raw=plain,
        )
        if not event.provider_trade_no or not event.order_no:
            raise PaymentError("微信回调缺关键单号")
        return event

    async def query_status(self, order_no: str) -> WebhookEvent | None:
        path = f"/v3/pay/transactions/out-trade-no/{order_no}?mchid={self._mchid}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    WXPAY_API + path,
                    headers={
                        "Authorization": build_authorization(
                            self._mchid, self._serial_no, self._private_key, "GET", path
                        )
                    },
                )
        except (httpx.ConnectError, httpx.TimeoutException):
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        state = str(data.get("trade_state", ""))
        amount = data.get("amount") or {}
        return WebhookEvent(
            provider="wechat",
            provider_trade_no=str(data.get("transaction_id") or ""),
            order_no=order_no,
            amount_cents=int(amount.get("total") or 0),
            status="success" if state == "SUCCESS" else "failed",
            raw=data,
        )


def verify_platform_signature(  # for completeness in tests/consumers
    serial_no: str, cert_pem: str, message: str, signature_b64: str
) -> bool:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    if f"{cert.serial_number:X}" != serial_no:
        return False
    pub = cert.public_key()
    if not isinstance(pub, rsa.RSAPublicKey):
        return False
    try:
        pub.verify(
            base64.b64decode(signature_b64),
            message.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False
