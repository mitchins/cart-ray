import asyncio
import sqlite3

import pytest

from cartray.canonical import CanonicalItem, parse_projection_items
from cartray.catalogue import Catalogue
from cartray.errors import CheckoutValidationError, IdempotencyConflict
from cartray.models import CheckoutRequest


def valid_request(catalogue, request_id: str = "request-1"):
    return CheckoutRequest(
        checkout_request_id=request_id,
        manifest_version=catalogue.version,
        items=(
            CanonicalItem("TEST-FREE", 1),
            CanonicalItem("TEST-BUNDLE", 1),
            CanonicalItem("TEST-TEMPLATE", 1),
        ),
    )


def checkout(service, request):
    return asyncio.run(service.checkout(request))


def test_mixed_cart_creates_immutable_order_and_trusted_gateway_spec(checkout_service):
    service, gateway = checkout_service
    redirect = checkout(service, valid_request(service.catalogue))

    assert redirect.url.startswith("https://checkout.invalid/")
    assert len(gateway.requests) == 1
    spec = gateway.requests[0]
    assert spec.line_items == (
        ("price_fixture_bundle", 1),
        ("price_fixture_free", 1),
        ("price_fixture_template", 1),
    )
    assert parse_projection_items(spec.metadata) == (
        CanonicalItem("TEST-BUNDLE", 1),
        CanonicalItem("TEST-FREE", 1),
        CanonicalItem("TEST-TEMPLATE", 1),
    )

    order = service.store.order_row(spec.order_id)
    assert order["subtotal_minor"] == 7500
    assert len(service.store.order_items(spec.order_id)) == 3
    assert [event["event_type"] for event in service.store.outbox_events(spec.order_id)] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
    ]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        service.store.connection.execute("UPDATE orders SET currency = 'usd' WHERE order_id = ?", (spec.order_id,))


def test_same_request_reuses_the_logical_checkout_and_changed_body_conflicts(checkout_service):
    service, gateway = checkout_service
    request = valid_request(service.catalogue)
    first = checkout(service, request)
    second = checkout(service, request)
    assert second == first
    assert len(gateway.requests) == 1

    changed = CheckoutRequest(
        checkout_request_id=request.checkout_request_id,
        manifest_version=service.catalogue.version,
        items=(CanonicalItem("TEST-FREE", 1),),
    )
    with pytest.raises(IdempotencyConflict):
        checkout(service, changed)


def test_expired_creation_lease_reuses_the_persisted_immutable_order(checkout_service):
    service, gateway = checkout_service
    request = valid_request(service.catalogue, "recoverable-request")
    abandoned = service._reconstruct_order(request)
    asyncio.run(service.store.start_or_load(abandoned, nonce="original-nonce", now=0))
    service.catalogue = Catalogue("sha256:changed-catalogue", {})

    redirect = checkout(service, request)
    spec = gateway.requests[0]
    assert redirect.session_id == "cr_test_000001"
    assert spec.order_id == abandoned.order_id
    assert spec.metadata["cr_nonce"] == "original-nonce"


def test_browser_payload_cannot_carry_financial_or_fulfilment_facts(checkout_service):
    service, _ = checkout_service
    payload = {
        "checkout_request_id": "request-2",
        "manifest_version": service.catalogue.version,
        "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1}],
        "amount_minor": 1,
    }
    with pytest.raises(CheckoutValidationError, match="unsupported"):
        CheckoutRequest.from_payload(payload)


def test_stale_manifest_unknown_item_duplicate_and_quantity_are_rejected(checkout_service):
    service, _ = checkout_service
    with pytest.raises(CheckoutValidationError, match="refresh"):
        checkout(service, CheckoutRequest("stale", "sha256:stale", (CanonicalItem("TEST-TEMPLATE", 1),)))
    with pytest.raises(CheckoutValidationError, match="unknown"):
        checkout(service, CheckoutRequest("unknown", service.catalogue.version, (CanonicalItem("UNKNOWN", 1),)))
    with pytest.raises(CheckoutValidationError, match="duplicate"):
        checkout(
            service,
            CheckoutRequest(
                "duplicates",
                service.catalogue.version,
                (CanonicalItem("TEST-TEMPLATE", 1), CanonicalItem("TEST-TEMPLATE", 1)),
            ),
        )
    with pytest.raises(CheckoutValidationError, match="quantity"):
        checkout(service, CheckoutRequest("quantity", service.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 2),)))
