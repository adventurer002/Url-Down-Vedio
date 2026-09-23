"""Phase8: signatures, idempotent webhooks, amount guard, reconcile."""

import base64
import json
import time
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.models import Order, Payment
from backend.app.providers.alipay_pay import (
    load_private_key as ali_priv,
)
from backend.app.providers.alipay_pay import (
    load_public_key as ali_pub,
)
from backend.app.providers.alipay_pay import (
    sign_params,
    verify_params,
)
from backend.app.providers.payment import PaymentError, WebhookEvent
from backend.app.providers.stripe_pay import sign_test_header, verify_signature
from backend.app.providers.wechat_pay import WechatPayProvider, decrypt_resource
from backend.app.services import billing as billing_service

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


@pytest.fixture()
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DB_URL)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


def _rsa_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv, pub


def test_stripe_signature_roundtrip() -> None:
    payload = json.dumps({"id": "evt_1", "type": "checkout.session.completed"}).encode()
    header = sign_test_header(payload, "whsec_test")
    event = verify_signature(payload, header, "whsec_test")
    assert event["id"] == "evt_1"
    with pytest.raises(PaymentError):
        verify_signature(payload, header, "whsec_wrong")
    with pytest.raises(PaymentError):
        verify_signature(payload + b" ", header, "whsec_test")
    old = sign_test_header(payload, "whsec_test", ts=int(time.time()) - 3600)
    with pytest.raises(PaymentError):
        verify_signature(payload, old, "whsec_test")


def test_alipay_rsa2_roundtrip() -> None:
    priv_pem, pub_pem = _rsa_keypair()
    priv, pub = ali_priv(priv_pem), ali_pub(pub_pem)
    params = {"app_id": "2024", "out_trade_no": "NO123", "total_amount": "9.90"}
    signed = sign_params(params, priv)
    assert verify_params(signed, pub)
    tampered = dict(signed, total_amount="99.90")
    assert not verify_params(tampered, pub)


def test_wechat_gcm_roundtrip() -> None:
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    api_key = os.urandom(32).hex()[:32]
    plain = {"out_trade_no": "NO456", "trade_state": "SUCCESS", "mchid": "M1"}
    nonce = os.urandom(12)
    aesgcm = AESGCM(api_key.encode())
    ct = aesgcm.encrypt(nonce, json.dumps(plain).encode(), b"")
    resource = {
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
        "associated_data": "",
    }
    assert decrypt_resource(api_key, resource) == plain
    with pytest.raises(PaymentError):
        decrypt_resource("0" * 32, resource)


def test_wechat_provider_verify_shape() -> None:
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    api_key = os.urandom(32).hex()[:32]
    provider = WechatPayProvider("M1", "APP1", "SER1", priv_pem, api_key, "https://n/cb")
    plain = {
        "mchid": "M1",
        "out_trade_no": "ORDER1",
        "transaction_id": "TXN1",
        "trade_state": "SUCCESS",
        "amount": {"total": 990},
    }
    nonce = os.urandom(12)
    ct = AESGCM(api_key.encode()).encrypt(nonce, json.dumps(plain).encode(), b"")
    body = json.dumps(
        {
            "resource": {
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ct).decode(),
            }
        }
    ).encode()
    event = provider.verify_webhook({}, body)
    assert event.status == "success"
    assert event.order_no == "ORDER1" and event.amount_cents == 990


async def _buyer(maker: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    from backend.app.services import auth_service as auth

    async with maker() as s:
        from backend.app.db.seed import seed_plans

        await seed_plans(s)
        user, _, _ = await auth.register_user(s, "pay@example.com", "password123", "p")
        order = await billing_service.create_order(s, user.id, "monthly")
        return order.order_no, order.amount_cents


def _event(order_no: str, amount: int, trade: str = "TXN-1") -> WebhookEvent:
    return WebhookEvent(
        provider="stripe", provider_trade_no=trade, order_no=order_no,
        amount_cents=amount, status="success", raw={},
    )


async def test_webhook_grant_then_duplicate(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    order_no, amount = await _buyer(maker)
    async with maker() as s:
        result, order = await billing_service.apply_webhook_event(
            s, _event(order_no, amount)
        )
        assert result == "granted"
        assert order is not None and order.status == "paid"
        result2, _ = await billing_service.apply_webhook_event(
            s, _event(order_no, amount)
        )
        assert result2 == "duplicate"
        count = (await s.execute(sa.select(sa.func.count()).select_from(Payment))).scalar_one()
        assert count == 1


async def test_webhook_amount_mismatch_ignored(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    order_no, amount = await _buyer(maker)
    async with maker() as s:
        result, order = await billing_service.apply_webhook_event(
            s, _event(order_no, amount + 1)
        )
        assert result == "ignored"
        assert order is not None and order.status == "pending"
        payment = (await s.execute(sa.select(Payment))).scalar_one()
        assert payment.status == "failed"


async def test_webhook_unknown_order_ignored(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as s:
        result, order = await billing_service.apply_webhook_event(
            s, _event("NOPE-000", 100)
        )
        assert result == "ignored" and order is None


async def test_reconcile_picks_up_paid(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    order_no, amount = await _buyer(maker)

    class FakeChannel:
        async def query_status(self, order_no_arg: str) -> WebhookEvent | None:
            assert order_no_arg == order_no
            return _event(order_no, amount, trade="TXN-R")

    monkeypatch.setattr(billing_service, "get_provider", lambda name: FakeChannel())
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "stripe_enabled", True)
    async with maker() as s:
        order = (
            await s.execute(sa.select(Order).where(Order.order_no == order_no))
        ).scalar_one()
        result, updated = await billing_service.reconcile_order(s, order)
        assert result == "granted"
        assert updated.status == "paid"
