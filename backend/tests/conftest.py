"""Keep legacy offline tests independent of the developer's private .env.

Allowance tests explicitly opt in and cover the new mandatory-auth path.
"""
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_product_settings(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_REQUIRED", False)
    monkeypatch.setattr(settings, "WECHAT_MP_STATIC_QR_URL", "")
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", False)
