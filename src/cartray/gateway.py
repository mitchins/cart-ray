from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import CheckoutRedirect, CheckoutSpec


class PaymentGateway(Protocol):
    def create_checkout(self, spec: CheckoutSpec) -> CheckoutRedirect: ...


@dataclass
class FakePaymentGateway:
    """Tiny deterministic test seam, intentionally not a Stripe simulator."""

    requests: list[CheckoutSpec] = field(default_factory=list)
    redirects: dict[str, CheckoutRedirect] = field(default_factory=dict)

    def create_checkout(self, spec: CheckoutSpec) -> CheckoutRedirect:
        self.requests.append(spec)
        return self.redirects.setdefault(
            spec.idempotency_key,
            CheckoutRedirect(
                session_id=f"cr_test_{len(self.redirects) + 1:06d}",
                url=f"https://checkout.invalid/{spec.order_id}",
            ),
        )
