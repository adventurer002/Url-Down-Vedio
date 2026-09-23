"""Stripe via raw HTTPS (Checkout Sessions + webhook HMAC). No SDK needed."""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.app.providers.payment import (
    PaymentError,
    PayParams,
    WebhookEvent,
)

STRIPE_API = "https://api.stripe.com/v1"


def verify_signature(payload: bytes, header: str, secret: str, tolerance: int = 300) -> dict[str, Any]:
    """Raise PaymentError(400) when the Stripe-Signature header is invalid."""
    items: dict[str, list[str]] = {}
    for part in header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        items.setdefault(k.strip(), []).append(v)
    timestamps = items.get("t", [])
    signatures = items.get("v1", [])
    if not timestamps or not signatures:
        raise PaymentError("Stripe 签名缺失")
    try:
        ts = int(timestamps[0])
    except ValueError:
        raise PaymentError("Stripe 签名时间非法")
    if abs(time.time() - ts) > tolerance:
        raise PaymentError("Stripe 签名已过期")
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise PaymentError("Stripe 签名校验失败")
    event: dict[str, Any] = json.loads(payload.decode())
    return event


def sign_test_header(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class StripeProvider:
    provider = "stripe"

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        success_url: str,
        cancel_url: str,
    ) -> None:
        if not secret_key or not webhook_secret:
            raise PaymentError("Stripe 未配置", 500)
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._success_url = success_url
        self._cancel_url = cancel_url

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret_key}"}

    async def create_payment(
        self, order_no: str, amount_cents: int, currency: str, subject: str
    ) -> PayParams:
        form = {
            "mode": "payment",
            "success_url": self._success_url,
            "cancel_url": self._cancel_url,
            "client_reference_id": order_no,
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": subject,
            "line_items[0][quantity]": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{STRIPE_API}/checkout/sessions",
                    headers=self._auth(),
                    content=urlencode(form),
                )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise PaymentError(f"Stripe 连接失败: {e}", 502)
        if resp.status_code != 200:
            raise PaymentError(f"Stripe 下单失败 {resp.status_code}", 502)
        data = resp.json()
        if not data.get("url"):
            raise PaymentError("Stripe 未返回支付链接", 502)
        return PayParams(provider="stripe", pay_url=str(data["url"]))

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        sig = headers.get("stripe-signature", "")
        event = verify_signature(body, sig, self._webhook_secret)
        event_type = str(event.get("type", ""))
        obj = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
        if event_type == "checkout.session.completed":
            return WebhookEvent(
                provider="stripe",
                provider_trade_no=str(obj.get("payment_intent") or obj.get("id")),
                order_no=str(obj.get("client_reference_id") or ""),
                amount_cents=int(obj.get("amount_total") or 0),
                status="success",
                raw=event,
            )
        if event_type in ("checkout.session.expired", "payment_intent.payment_failed"):
            return WebhookEvent(
                provider="stripe",
                provider_trade_no=str(obj.get("payment_intent") or obj.get("id")),
                order_no=str(obj.get("client_reference_id") or ""),
                amount_cents=int(obj.get("amount_total") or 0),
                status="failed",
                raw=event,
            )
        raise PaymentError(f"Stripe 事件暂不处理: {event_type}")

    async def query_status(self, order_no: str) -> WebhookEvent | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{STRIPE_API}/checkout/sessions",
                    headers=self._auth(),
                    params={"client_reference_id": order_no, "limit": "1"},
                )
        except (httpx.ConnectError, httpx.TimeoutException):
            return None
        if resp.status_code != 200:
            return None
        items = resp.json().get("data") or []
        if not items:
            return None
        s = items[0]
        paid = s.get("payment_status") == "paid"
        return WebhookEvent(
            provider="stripe",
            provider_trade_no=str(s.get("payment_intent") or s.get("id")),
            order_no=order_no,
            amount_cents=int(s.get("amount_total") or 0),
            status="success" if paid else "failed",
            raw=s,
        )
