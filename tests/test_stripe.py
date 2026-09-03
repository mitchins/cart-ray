from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import pytest

from cartray.canonical import CanonicalItem, projection_metadata
from cartray.errors import CatalogueValidationError
from cartray.models import CheckoutSpec
from cartray.stripe import (
    STRIPE_API_VERSION,
    CheckoutMetadataSealer,
    CheckoutMetadataVerifier,
    ProjectionSealError,
    StripeApiClient,
    StripeApiError,
    StripeCheckoutGateway,
    StripeCheckoutSessionRetriever,
    StripePriceResolver,
    signature_payload,
)


@dataclass
class RecordingTransport:
    responses: list[tuple[int, dict[str, object]]]
    requests: list[tuple[str, str, dict[str, str], str | None]] = field(default_factory=list)

    async def request(self, method, path, *, headers, body=None):
        self.requests.append((method, path, dict(headers), body))
        return self.responses.pop(0)


@dataclass(frozen=True)
class FixtureSigner:
    async def sign(self, payload: bytes) -> bytes:
        return b"fixture-signature:" + payload[-8:]


@dataclass(frozen=True)
class EmptySigner:
    async def sign(self, _payload: bytes) -> bytes:
        return b""


@dataclass(frozen=True)
class ExpectedVerifier:
    expected_payload: bytes

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        return payload == self.expected_payload and signature == b"fixture-verifier-signature"


def test_metadata_sealing_rejects_an_upstream_failure_without_a_checkout_redirect():
    metadata = projection_metadata(
        order_id="cr_order_123",
        catalogue_version="sha256:catalogue",
        items=(CanonicalItem("TEST-TEMPLATE", 1),),
        nonce="nonce-123",
    )
    sealer = CheckoutMetadataSealer(environment="test", key_id="test-key-1", signer=EmptySigner())

    with pytest.raises(ProjectionSealError, match="empty"):
        asyncio.run(sealer.seal(session_id="cs_test_123", metadata=metadata))


def test_metadata_verification_rejects_unknown_cart_ray_fields():
    metadata = projection_metadata(
        order_id="cr_order_123",
        catalogue_version="sha256:catalogue",
        items=(CanonicalItem("TEST-TEMPLATE", 1),),
        nonce="nonce-123",
    )
    metadata["cr_kid"] = "fixture-key"
    payload = signature_payload(session_id="cs_test_123", environment="test", metadata=metadata)
    metadata["cr_signature"] = base64.urlsafe_b64encode(b"fixture-verifier-signature").rstrip(b"=").decode()
    verifier = CheckoutMetadataVerifier("test", {"fixture-key": ExpectedVerifier(payload)})

    assert asyncio.run(verifier.verify(session_id="cs_test_123", metadata=metadata)) == (
        CanonicalItem("TEST-TEMPLATE", 1),
    )
    with_unknown_field = {**metadata, "cr_unexpected": "nope"}
    with pytest.raises(ProjectionSealError, match="unknown"):
        asyncio.run(verifier.verify(session_id="cs_test_123", metadata=with_unknown_field))


def test_metadata_verification_rejects_a_signed_noncanonical_rechunking():
    metadata = projection_metadata(
        order_id="cr_order_123",
        catalogue_version="sha256:catalogue",
        items=(CanonicalItem("TEST-BUNDLE", 1), CanonicalItem("TEST-TEMPLATE", 1)),
        nonce="nonce-123",
    )
    metadata["cr_kid"] = "fixture-key"
    payload = signature_payload(session_id="cs_test_123", environment="test", metadata=metadata)
    metadata["cr_signature"] = base64.urlsafe_b64encode(b"fixture-verifier-signature").rstrip(b"=").decode()
    rechunked = {
        **metadata,
        "cr_chunk_count": "2",
        "cr_items_01": "TEST-BUNDLE:1",
        "cr_items_02": "TEST-TEMPLATE:1",
    }
    verifier = CheckoutMetadataVerifier("test", {"fixture-key": ExpectedVerifier(payload)})

    with pytest.raises(ProjectionSealError, match="non-canonical"):
        asyncio.run(verifier.verify(session_id="cs_test_123", metadata=rechunked))


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
        items=(CanonicalItem("TEST-SUPPORT-HOURS", 5),),
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
                line_items=(("price_test_support_hours", 5),),
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
    assert created_form["success_url"] == ["https://store.invalid/success"]
    assert created_form["cancel_url"] == ["https://store.invalid/cancel"]
    assert not any(key.startswith("metadata[") for key in created_form)
    assert created_form["line_items[0][price]"] == ["price_test_support_hours"]
    assert created_form["line_items[0][quantity]"] == ["5"]
    assert transport.requests[0][2]["Idempotency-Key"] == "cartray-checkout-v1:cr_order_123"

    session_metadata = parse_qs(transport.requests[1][3] or "")
    payment_intent_metadata = parse_qs(transport.requests[2][3] or "")
    assert session_metadata == payment_intent_metadata
    assert session_metadata["metadata[cr_items_01]"] == ["TEST-SUPPORT-HOURS:5"]
    assert session_metadata["metadata[cr_kid]"] == ["test-key-1"]
    assert session_metadata["metadata[cr_signature]"][0]


def test_stripe_checkout_keeps_session_metadata_canonical_when_no_payment_intent_exists():
    transport = RecordingTransport(
        [
            (
                200,
                {
                    "id": "cs_test_free",
                    "url": "https://checkout.stripe.test/c/pay/cs_test_free",
                    "payment_intent": None,
                },
            ),
            (200, {"id": "cs_test_free"}),
        ]
    )
    metadata = projection_metadata(
        order_id="cr_order_free",
        catalogue_version="sha256:catalogue",
        items=(CanonicalItem("TEST-FREE", 1),),
        nonce="nonce-free",
    )
    gateway = StripeCheckoutGateway(
        StripeApiClient("rk_test_fixture", transport),
        CheckoutMetadataSealer(environment="test", key_id="test-key-1", signer=FixtureSigner()),
    )

    redirect = asyncio.run(
        gateway.create_checkout(
            CheckoutSpec(
                order_id="cr_order_free",
                idempotency_key="cartray-checkout-v1:cr_order_free",
                line_items=(("price_test_free", 1),),
                success_url="https://store.invalid/success",
                cancel_url="https://store.invalid/cancel",
                metadata=metadata,
            )
        )
    )

    assert redirect.session_id == "cs_test_free"
    assert [request[1] for request in transport.requests] == [
        "/v1/checkout/sessions",
        "/v1/checkout/sessions/cs_test_free",
    ]
    assert {request[2]["Stripe-Version"] for request in transport.requests} == {STRIPE_API_VERSION}
    session_metadata = parse_qs(transport.requests[1][3] or "")
    assert session_metadata["metadata[cr_order_id]"] == ["cr_order_free"]
    assert session_metadata["metadata[cr_signature]"][0]


def test_stripe_price_resolver_requires_one_active_one_off_price():
    transport = RecordingTransport(
        [(200, {"data": [{"id": "price_test_template", "unit_amount": 2500, "currency": "aud", "recurring": None}]})]
    )
    resolver = StripePriceResolver(StripeApiClient("rk_test_fixture", transport))

    price = asyncio.run(resolver.resolve("cr_test_template"))

    assert price.stripe_price_id == "price_test_template"
    assert transport.requests[0][1] == "/v1/prices?active=true&lookup_keys%5B%5D=cr_test_template&limit=2"


def test_stripe_api_version_pin_cannot_be_overridden_by_a_request_caller():
    assert STRIPE_API_VERSION == "2025-09-30.clover"

    transport = RecordingTransport([(200, {})])
    client = StripeApiClient("rk_test_fixture", transport)

    assert asyncio.run(client._request("GET", "/v1/test", headers={"Stripe-Version": "2020-08-27"})) == {}
    assert transport.requests[0][2]["Stripe-Version"] == STRIPE_API_VERSION


@pytest.mark.parametrize(
    "prices",
    [[], [{"id": "price_1", "unit_amount": 1, "currency": "aud", "recurring": None}] * 2],
)
def test_stripe_price_resolver_rejects_missing_or_ambiguous_lookup_keys(prices):
    resolver = StripePriceResolver(StripeApiClient("rk_test_fixture", RecordingTransport([(200, {"data": prices})])))

    with pytest.raises(CatalogueValidationError, match="exactly one"):
        asyncio.run(resolver.resolve("cr_test_missing"))


def test_checkout_session_retrieval_reconciles_every_line_item_page():
    transport = RecordingTransport(
        [
            (
                200,
                {
                    "id": "cs_test_retrieve",
                    "livemode": False,
                    "mode": "payment",
                    "status": "complete",
                    "payment_status": "paid",
                    "amount_total": 7500,
                    "currency": "aud",
                    "metadata": {"cr_order_id": "cr_order_123"},
                },
            ),
            (
                200,
                {
                    "data": [
                        {
                            "id": "li_one",
                            "quantity": 1,
                            "price": {"id": "price_one", "unit_amount": 2500, "currency": "aud"},
                        }
                    ],
                    "has_more": True,
                },
            ),
            (
                200,
                {
                    "data": [
                        {
                            "id": "li_two",
                            "quantity": 5,
                            "price": {"id": "price_two", "unit_amount": 1000, "currency": "aud"},
                        }
                    ],
                    "has_more": False,
                },
            ),
        ]
    )

    session = asyncio.run(
        StripeCheckoutSessionRetriever(StripeApiClient("rk_test_fixture", transport)).retrieve("cs_test_retrieve")
    )

    assert [(item.price_id, item.quantity) for item in session.line_items] == [
        ("price_one", 1),
        ("price_two", 5),
    ]
    assert transport.requests[-1][1].endswith("line_items?limit=100&starting_after=li_one")


def test_checkout_session_retrieval_rejects_duplicate_line_ids_in_one_page():
    transport = RecordingTransport(
        [
            (
                200,
                {
                    "id": "cs_test_duplicates",
                    "livemode": False,
                    "mode": "payment",
                    "status": "complete",
                    "payment_status": "paid",
                    "amount_total": 5000,
                    "currency": "aud",
                    "metadata": {},
                },
            ),
            (
                200,
                {
                    "data": [
                        {
                            "id": "li_duplicate",
                            "quantity": 1,
                            "price": {"id": "price_one", "unit_amount": 2500, "currency": "aud"},
                        },
                        {
                            "id": "li_duplicate",
                            "quantity": 1,
                            "price": {"id": "price_two", "unit_amount": 2500, "currency": "aud"},
                        },
                    ],
                    "has_more": False,
                },
            ),
        ]
    )

    with pytest.raises(StripeApiError, match="duplicates"):
        asyncio.run(
            StripeCheckoutSessionRetriever(StripeApiClient("rk_test_fixture", transport)).retrieve("cs_test_duplicates")
        )
