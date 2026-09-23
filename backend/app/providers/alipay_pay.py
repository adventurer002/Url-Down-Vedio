"""Alipay page pay (redirect URL) + async notify RSA2 verify."""

import base64
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.app.providers.payment import (
    PaymentError,
    PayParams,
    WebhookEvent,
)

ALIPAY_GATEWAY = "https://openapi.alipay.com/gateway.do"


def load_private_key(pem: str):  # type: ignore[no-untyped-def]
    return serialization.load_pem_private_key(pem.encode(), password=None)


def load_public_key(pem: str):  # type: ignore[no-untyped-def]
    return serialization.load_pem_public_key(pem.encode())


def _sign_content(params: dict[str, str], private_key: Any) -> str:
    content = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] != "")
    signature = private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def sign_params(params: dict[str, str], private_key: Any) -> dict[str, str]:
    signed = dict(params)
    signed["sign"] = _sign_content(params, private_key)
    return signed


def verify_params(params: dict[str, str], public_key: Any) -> bool:
    sign = params.get("sign", "")
    if not isinstance(public_key, rsa.RSAPublicKey):
        return False
    content = "&".join(f"{k}={params[k]}" for k in sorted(params) if k not in ("sign", "sign_type") and params[k] != "")
    try:
        public_key.verify(
            base64.b64decode(sign), content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )
        return True
    except (InvalidSignature, ValueError):
        return False


class AlipayProvider:
    """Page pay. Currency must be CNY; amounts passed in yuan (2 decimals)."""

    provider = "alipay"

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        alipay_public_key_pem: str,
        notify_url: str,
        return_url: str,
    ) -> None:
        if not (app_id and private_key_pem and alipay_public_key_pem):
            raise PaymentError("支付宝未配置", 500)
        self._app_id = app_id
        self._private_key = load_private_key(private_key_pem)
        self._public_key = load_public_key(alipay_public_key_pem)
        self._notify_url = notify_url
        self._return_url = return_url

    def _base_params(self, method: str) -> dict[str, str]:
        return {
            "app_id": self._app_id,
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "notify_url": self._notify_url,
        }

    async def create_payment(
        self, order_no: str, amount_cents: int, currency: str, subject: str
    ) -> PayParams:
        if currency.upper() != "CNY":
            raise PaymentError("支付宝仅支持人民币", 400)
        biz = {
            "out_trade_no": order_no,
            "product_code": "FAST_INSTANT_TRADE_PAY",
            "total_amount": f"{amount_cents / 100:.2f}",
            "subject": subject[:256],
        }
        params = self._base_params("alipay.trade.page.pay")
        params["biz_content"] = json.dumps(biz, separators=(",", ":"))
        params["return_url"] = self._return_url
        signed = sign_params(params, self._private_key)
        url = f"{ALIPAY_GATEWAY}?{urlencode(signed, quote_via=quote_plus)}"
        return PayParams(provider="alipay", pay_url=url)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        from urllib.parse import parse_qsl

        params = {k: v for k, v in parse_qsl(body.decode(), keep_blank_values=True)}
        if not verify_params(params, self._public_key):
            raise PaymentError("支付宝签名校验失败")
        if params.get("app_id") != self._app_id:
            raise PaymentError("支付宝回调应用不符")
        status = str(params.get("trade_status", ""))
        try:
            amount_cents = round(float(params.get("total_amount", "0")) * 100)
        except ValueError:
            raise PaymentError("支付宝回调金额非法")
        event = WebhookEvent(
            provider="alipay",
            provider_trade_no=str(params.get("trade_no") or ""),
            order_no=str(params.get("out_trade_no") or ""),
            amount_cents=amount_cents,
            status="success" if status == "TRADE_SUCCESS" else "failed",
            raw=params,
        )
        if not event.provider_trade_no or not event.order_no:
            raise PaymentError("支付宝回调缺关键单号")
        return event

    async def query_status(self, order_no: str) -> WebhookEvent | None:
        params = self._base_params("alipay.trade.query")
        params["biz_content"] = json.dumps({"out_trade_no": order_no}, separators=(",", ":"))
        signed = sign_params(params, self._private_key)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(ALIPAY_GATEWAY, data=signed)
        except (httpx.ConnectError, httpx.TimeoutException):
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()["alipay_trade_query_response"]
        except (KeyError, ValueError):
            return None
        if str(data.get("code")) != "10000":
            return None
        status = str(data.get("trade_status", ""))
        try:
            amount_cents = round(float(data.get("total_amount", "0")) * 100)
        except ValueError:
            return None
        return WebhookEvent(
            provider="alipay",
            provider_trade_no=str(data.get("trade_no") or ""),
            order_no=order_no,
            amount_cents=amount_cents,
            status="success" if status == "TRADE_SUCCESS" else "failed",
            raw=data,
        )
