from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import pytest

from cartray.canonical import CanonicalItem, projection_metadata
from cartray.errors import CatalogueValidationError
from cartray.models import CheckoutSpec
from cartray.stripe import CheckoutMetadataSealer, StripeApiClient, StripeCheckoutGateway, StripePriceResolver


@dataclass
class RecordingTransport:
    responses: list[tuple[int, dict[str, object]]]
    requests: list[tuple[str, str, dict[str, str], str | None]] = field(default_factory=list)

    async def request(self, method, path, *, headers, body=None):
        self.requests.append((method, path, dict(headers), body))
        return self.responses.pop(0)


@dataclass(frozen=True)
class FixtureSigner:
    def sign(self, payload: bytes) -> bytes:
        return b"fixture-signature:" + payload[-8:]


def test_stripe_checkout_is_sealed_after_session_creation_and_before_redirect():
    transport = RecordingTransport(
        [
            (
                200,
                {
                    "id": "cs_test_123",
                    "url": "https://checkout.stripe.test/c/pay/cs_test_123",
                    "payment_intent": "pi_123",
                },
            ),
            (200, {"id": "cs_test_123"}),
            (200, {"id": "pi_123"}),
        ]
    )
    metadata = projection_metadata(
        order_id="cr_order_123",
        catalogue_version="sha256:catalogue",
        items=(CanonicalItem("TEST-TEMPLATE", 1),),
        nonce="nonce-123",
    )
    gateway = StripeCheckoutGateway(
        StripeApiClient("rk_test_fixture", transport),
        CheckoutMetadataSealer(environment="test", key_id="test-key-1", signer=FixtureSigner()),
    )
    redirect = asyncio.run(
        gateway.create_checkout(
            CheckoutSpec(
                order_id="cr_order_123",
                idempotency_key="cartray-checkout-v1:cr_order_123",
                line_items=(("price_test_template", 1),),
                success_url="https://store.invalid/success",
                cancel_url="https://store.invalid/cancel",
                metadata=metadata,
            )
        )
    )

    assert redirect.session_id == "cs_test_123"
    assert [request[1] for request in transport.requests] == [
        "/v1/checkout/sessions",
        "/v1/checkout/sessions/cs_test_123",
        "/v1/payment_intents/pi_123",
    ]
    created_form = parse_qs(transport.requests[0][3] or "")
    assert not any(key.startswith("metadata[") for key in created_form)
    assert created_form["line_items[0][price]"] == ["price_test_template"]
    assert transport.requests[0][2]["Idempotency-Key"] == "cartray-checkout-v1:cr_order_123"

    session_metadata = parse_qs(transport.requests[1][3] or "")
    payment_intent_metadata = parse_qs(transport.requests[2][3] or "")
    assert session_metadata == payment_intent_metadata
    assert session_metadata["metadata[cr_kid]"] == ["test-key-1"]
    assert session_metadata["metadata[cr_signature]"][0]


def test_stripe_price_resolver_requires_one_active_one_off_price():
    transport = RecordingTransport(
        [(200, {"data": [{"id": "price_test_template", "unit_amount": 2500, "currency": "aud", "recurring": None}]})]
    )
    resolver = StripePriceResolver(StripeApiClient("rk_test_fixture", transport))

    price = asyncio.run(resolver.resolve("cr_test_template"))

    assert price.stripe_price_id == "price_test_template"
    assert transport.requests[0][1] == "/v1/prices?active=true&lookup_keys%5B%5D=cr_test_template&limit=2"


@pytest.mark.parametrize(
    "prices",
    [[], [{"id": "price_1", "unit_amount": 1, "currency": "aud", "recurring": None}] * 2],
)
def test_stripe_price_resolver_rejects_missing_or_ambiguous_lookup_keys(prices):
    resolver = StripePriceResolver(StripeApiClient("rk_test_fixture", RecordingTransport([(200, {"data": prices})])))

    with pytest.raises(CatalogueValidationError, match="exactly one"):
        asyncio.run(resolver.resolve("cr_test_missing"))
