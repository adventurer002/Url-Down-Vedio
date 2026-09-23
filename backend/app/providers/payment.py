"""Payment abstraction. Money code never trusts frontend amounts."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PayParams:
    """What the frontend needs to collect money. Exactly one of url/qr/client_secret."""

    provider: str
    pay_url: str | None = None
    qr_code: str | None = None
    client_secret: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEvent:
    provider: str
    provider_trade_no: str
    order_no: str
    amount_cents: int
    status: str  # "success" | "failed"
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class PaymentProvider(Protocol):
    provider: str

    async def create_payment(
        self, order_no: str, amount_cents: int, currency: str, subject: str
    ) -> PayParams: ...

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent: ...

    async def query_status(self, order_no: str) -> WebhookEvent | None: ...
