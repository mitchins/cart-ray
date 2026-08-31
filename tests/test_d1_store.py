from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kinglet import MockD1Database

from cartray.canonical import CanonicalItem
from cartray.checkout import CheckoutService
from cartray.errors import CheckoutInProgress, IdempotencyConflict
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRedirect, CheckoutRequest
from cartray.store import D1OrderStore


@pytest.fixture
def d1_database():
    database = MockD1Database()
    migration = Path(__file__).parents[1] / "migrations" / "0001_commerce_kernel.sql"
    database.conn.executescript(migration.read_text())
    return database


def test_d1_store_persists_an_idempotent_checkout_and_outbox(fixture_catalogue, d1_database):
    database = d1_database
    gateway = FakePaymentGateway()
    service = CheckoutService(fixture_catalogue, D1OrderStore(database), gateway)
    request = CheckoutRequest(
        "d1-request-1",
        fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1),),
    )

    first = asyncio.run(service.checkout(request))
    second = asyncio.run(service.checkout(request))

    assert second == first
    assert len(gateway.requests) == 1
    events = asyncio.run(database.prepare("SELECT event_type FROM outbox ORDER BY id").all())
    assert [event["event_type"] for event in events.results] == ["OrderCreated", "CheckoutRedirectIssued"]


def test_d1_store_enforces_leases_and_recovers_expired_leases(fixture_catalogue, d1_database):
    database = d1_database
    service = CheckoutService(fixture_catalogue, D1OrderStore(database, lease_seconds=10), FakePaymentGateway())
    request = CheckoutRequest(
        "d1-lease-request",
        fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1),),
    )
    order = service._reconstruct_order(request)

    first = asyncio.run(service.store.start_or_load(order, nonce="first", now=100))
    with pytest.raises(CheckoutInProgress):
        asyncio.run(service.store.start_or_load(order, nonce="second", now=109))
    recovered = asyncio.run(service.store.start_or_load(order, nonce="second", now=110))

    assert first.owner is True
    assert recovered.owner is True
    assert recovered.order_id == first.order_id
    assert recovered.nonce == "first"


def test_d1_store_race_loser_rechecks_the_winner_lease(fixture_catalogue, d1_database, monkeypatch):
    database = d1_database
    store = D1OrderStore(database, lease_seconds=10)
    service = CheckoutService(fixture_catalogue, store, FakePaymentGateway())
    request = CheckoutRequest(
        "d1-race-request",
        fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1),),
    )
    winner = service._reconstruct_order(request)
    asyncio.run(store.start_or_load(winner, nonce="winner", now=100))

    original_checkout_start = store._checkout_start
    calls = 0

    async def hide_winner_once(request_id, fingerprint):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await original_checkout_start(request_id, fingerprint)

    monkeypatch.setattr(store, "_checkout_start", hide_winner_once)
    contender = service._reconstruct_order(request)

    with pytest.raises(CheckoutInProgress):
        asyncio.run(store.start_or_load(contender, nonce="contender", now=101))


def test_d1_store_rejects_a_conflicting_redirect_after_the_first_is_persisted(fixture_catalogue, d1_database):
    database = d1_database
    store = D1OrderStore(database)
    service = CheckoutService(fixture_catalogue, store, FakePaymentGateway())
    request = CheckoutRequest(
        "d1-redirect-request",
        fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1),),
    )
    order = service._reconstruct_order(request)
    asyncio.run(store.start_or_load(order, nonce="nonce", now=100))
    redirect = CheckoutRedirect("cs_test_first", "https://checkout.stripe.test/first")

    asyncio.run(store.attach_redirect(order.order_id, redirect, now=101))
    asyncio.run(store.attach_redirect(order.order_id, redirect, now=102))
    with pytest.raises(CheckoutInProgress):
        asyncio.run(
            store.attach_redirect(
                order.order_id, CheckoutRedirect("cs_test_other", "https://checkout.stripe.test/other")
            )
        )


def test_d1_store_rejects_reused_request_id_with_a_different_cart(fixture_catalogue, d1_database):
    store = D1OrderStore(d1_database)
    service = CheckoutService(fixture_catalogue, store, FakePaymentGateway())
    original_request = CheckoutRequest(
        "d1-conflict-request",
        fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1),),
    )
    conflicting_request = CheckoutRequest(
        "d1-conflict-request",
        fixture_catalogue.version,
        (CanonicalItem("TEST-BUNDLE", 1),),
    )

    asyncio.run(store.start_or_load(service._reconstruct_order(original_request), nonce="first", now=100))

    with pytest.raises(IdempotencyConflict):
        asyncio.run(store.start_or_load(service._reconstruct_order(conflicting_request), nonce="second", now=101))
