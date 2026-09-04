from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kinglet import MockD1Database

from cartray.canonical import CanonicalItem, parse_projection_items
from cartray.checkout import CheckoutService
from cartray.errors import CheckoutInProgress, IdempotencyConflict, SettlementInconsistency
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRedirect, CheckoutRequest
from cartray.store import D1OrderStore, SettlementRejection


@pytest.fixture
def d1_database():
    database = MockD1Database()
    for migration in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
        database.conn.executescript(migration.read_text())
    return database


def _pending_settlement(fixture_catalogue, d1_database, *, request_id: str, session_id: str, event_id: str):
    store = D1OrderStore(d1_database)
    gateway = FakePaymentGateway()
    service = CheckoutService(fixture_catalogue, store, gateway)
    request = CheckoutRequest(request_id, fixture_catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),))
    asyncio.run(service.checkout(request))
    order_id = gateway.requests[0].order_id
    asyncio.run(
        d1_database.prepare("UPDATE checkout_sessions SET external_session_id = ? WHERE order_id = ?")
        .bind(session_id, order_id)
        .run()
    )
    return (
        store,
        order_id,
        {
            "order_id": order_id,
            "session_id": session_id,
            "event_id": event_id,
            "payload": {"id": event_id},
            "payload_sha256": "sha256:first-delivery",
            "payment_status": "paid",
            "amount_total_minor": 2_500,
            "now": 123,
        },
    )


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


def test_d1_reconciliation_control_claims_and_defers_a_stale_pending_checkout(fixture_catalogue, d1_database):
    store = D1OrderStore(d1_database)
    gateway = FakePaymentGateway()
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    asyncio.run(
        checkout.checkout(
            CheckoutRequest("d1-reconciliation", fixture_catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),))
        )
    )
    spec = gateway.requests[0]
    asyncio.run(
        d1_database.prepare(
            "UPDATE checkout_sessions SET external_session_id = ?, updated_at = 0 WHERE order_id = ?"
        )
        .bind("cs_d1_reconciliation", spec.order_id)
        .run()
    )

    candidates = asyncio.run(
        store.claim_reconciliation_candidates(now=10_000, stale_before=6_400, limit=5, lease_seconds=300)
    )

    assert len(candidates) == 1
    assert candidates[0].order_id == spec.order_id
    assert asyncio.run(
        store.finish_reconciliation_retry(
            candidate=candidates[0],
            now=10_000,
            next_attempt_at=13_600,
            outcome="open",
            error=None,
            observed_status="open",
            observed_payment_status="unpaid",
            observed_amount_total_minor=2_500,
        )
    )
    row = asyncio.run(
        d1_database.prepare(
            "SELECT attempt_count, next_attempt_at, lease_token, last_outcome FROM checkout_reconciliations "
            "WHERE order_id = ?"
        )
        .bind(spec.order_id)
        .all()
    ).results[0]
    assert row == {"attempt_count": 1, "next_attempt_at": 13_600, "lease_token": None, "last_outcome": "open"}


def test_d1_reconciliation_confirmation_is_atomic_and_has_no_synthetic_stripe_event(fixture_catalogue, d1_database):
    store = D1OrderStore(d1_database)
    gateway = FakePaymentGateway()
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    asyncio.run(
        checkout.checkout(
            CheckoutRequest("d1-reconciliation-confirm", fixture_catalogue.version, (CanonicalItem("TEST-FREE", 1),))
        )
    )
    spec = gateway.requests[0]
    asyncio.run(
        d1_database.prepare(
            "UPDATE checkout_sessions SET external_session_id = ?, updated_at = 0 WHERE order_id = ?"
        )
        .bind("cs_d1_reconciliation_confirm", spec.order_id)
        .run()
    )
    candidate = asyncio.run(
        store.claim_reconciliation_candidates(now=10_000, stale_before=6_400, limit=5, lease_seconds=300)
    )[0]
    order = asyncio.run(store.load_order(spec.order_id))

    assert asyncio.run(
        store.confirm_reconciliation(
            candidate=candidate,
            payment_status="no_payment_required",
            amount_total_minor=0,
            now=10_000,
            items_digest=order.items_digest,
        )
    ) is False
    session = asyncio.run(
        d1_database.prepare(
            "SELECT settlement_state, settlement_event_id, stripe_payment_status FROM checkout_sessions "
            "WHERE order_id = ?"
        )
        .bind(spec.order_id)
        .all()
    ).results[0]
    assert session == {
        "settlement_state": "confirmed",
        "settlement_event_id": None,
        "stripe_payment_status": "no_payment_required",
    }
    outbox = asyncio.run(
        d1_database.prepare("SELECT event_type FROM outbox WHERE order_id = ? ORDER BY id").bind(spec.order_id).all()
    ).results
    assert [row["event_type"] for row in outbox] == ["OrderCreated", "CheckoutRedirectIssued", "OrderConfirmed"]
    events = asyncio.run(d1_database.prepare("SELECT count(*) AS count FROM stripe_events").all()).results
    assert events == [{"count": 0}]


@pytest.mark.parametrize("terminal", ["confirmed", "expired"])
def test_d1_reconciliation_rejects_a_stale_lease_after_another_worker_reclaims_it(
    fixture_catalogue, d1_database, terminal
):
    store = D1OrderStore(d1_database)
    gateway = FakePaymentGateway()
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    asyncio.run(
        checkout.checkout(
            CheckoutRequest(
                f"d1-reconciliation-stale-{terminal}",
                fixture_catalogue.version,
                (CanonicalItem("TEST-FREE", 1),),
            )
        )
    )
    spec = gateway.requests[0]
    asyncio.run(
        d1_database.prepare(
            "UPDATE checkout_sessions SET external_session_id = ?, updated_at = 0 WHERE order_id = ?"
        )
        .bind(f"cs_d1_reconciliation_stale_{terminal}", spec.order_id)
        .run()
    )
    first = asyncio.run(
        store.claim_reconciliation_candidates(now=10_000, stale_before=6_400, limit=5, lease_seconds=1)
    )[0]
    second = asyncio.run(
        store.claim_reconciliation_candidates(now=10_002, stale_before=6_402, limit=5, lease_seconds=300)
    )[0]
    order = asyncio.run(store.load_order(spec.order_id))

    with pytest.raises(SettlementInconsistency):
        if terminal == "confirmed":
            asyncio.run(
                store.confirm_reconciliation(
                    candidate=first,
                    payment_status="no_payment_required",
                    amount_total_minor=0,
                    now=10_002,
                    items_digest=order.items_digest,
                )
            )
        else:
            asyncio.run(
                store.expire_reconciliation(
                    candidate=first,
                    payment_status="unpaid",
                    amount_total_minor=0,
                    now=10_002,
                )
            )

    state = asyncio.run(
        d1_database.prepare("SELECT settlement_state FROM checkout_sessions WHERE order_id = ?")
        .bind(spec.order_id)
        .all()
    ).results[0]
    assert state == {"settlement_state": "pending"}
    audit = asyncio.run(
        d1_database.prepare("SELECT lease_token FROM checkout_reconciliations WHERE order_id = ?")
        .bind(spec.order_id)
        .all()
    ).results[0]
    assert audit == {"lease_token": second.lease_token}

    if terminal == "confirmed":
        assert asyncio.run(
            store.confirm_reconciliation(
                candidate=second,
                payment_status="no_payment_required",
                amount_total_minor=0,
                now=10_002,
                items_digest=order.items_digest,
            )
        ) is False
    else:
        assert asyncio.run(
            store.expire_reconciliation(
                candidate=second,
                payment_status="unpaid",
                amount_total_minor=0,
                now=10_002,
            )
        ) is False


def test_d1_store_persists_the_maximum_quantity_and_stripe_projection(fixture_catalogue, d1_database):
    gateway = FakePaymentGateway()
    service = CheckoutService(fixture_catalogue, D1OrderStore(d1_database), gateway)
    request = CheckoutRequest(
        "d1-support-hours-five",
        fixture_catalogue.version,
        (CanonicalItem("TEST-SUPPORT-HOURS", 5),),
    )

    asyncio.run(service.checkout(request))

    spec = gateway.requests[0]
    assert spec.line_items == (("price_fixture_support_hours", 5),)
    assert parse_projection_items(spec.metadata) == (CanonicalItem("TEST-SUPPORT-HOURS", 5),)
    items = asyncio.run(
        d1_database.prepare("SELECT product_key, quantity FROM order_items WHERE order_id = ?")
        .bind(spec.order_id)
        .all()
    )
    assert items.results == [{"product_key": "TEST-SUPPORT-HOURS", "quantity": 5}]


def test_d1_store_records_only_known_diagnostics_for_a_logical_received_event(d1_database):
    store = D1OrderStore(d1_database)
    event_id = "evt_diagnostic"
    session_id = "cs_diagnostic"
    payload_sha256 = "sha256:diagnostic"
    asyncio.run(
        store.begin_settlement_event(
            event_id=event_id,
            session_id=session_id,
            payload={"id": event_id},
            payload_sha256=payload_sha256,
        )
    )
    asyncio.run(
        store.record_settlement_rejection(
            event_id=event_id,
            session_id=session_id,
            rejection=SettlementRejection.PROJECTION,
        )
    )
    asyncio.run(
        store.record_settlement_rejection(
            event_id=event_id,
            session_id="cs_other",
            rejection=SettlementRejection.RECONCILIATION,
        )
    )
    asyncio.run(
        store.record_settlement_rejection(
            event_id=event_id,
            session_id=session_id,
            rejection=SettlementRejection.RECONCILIATION,
        )
    )
    row = asyncio.run(
        d1_database.prepare("SELECT processing_state, processing_error FROM stripe_events WHERE stripe_event_id = ?")
        .bind(event_id)
        .all()
    ).results[0]
    assert row == {"processing_state": "received", "processing_error": SettlementRejection.RECONCILIATION}
    with pytest.raises(ValueError, match="known operator diagnostic"):
        asyncio.run(
            store.record_settlement_rejection(
                event_id=event_id,
                session_id=session_id,
                rejection="arbitrary diagnostic",  # type: ignore[arg-type]
            )
        )


def test_d1_store_expires_a_known_pending_session_idempotently_without_outbox(fixture_catalogue, d1_database):
    store, order_id, settlement = _pending_settlement(
        fixture_catalogue,
        d1_database,
        request_id="d1-expiry",
        session_id="cs_d1_expiry",
        event_id="evt_d1_completion",
    )
    expiry = {
        "event_id": "evt_d1_expiry",
        "session_id": settlement["session_id"],
        "payload": {"id": "evt_d1_expiry", "type": "checkout.session.expired"},
        "payload_sha256": "sha256:expiry",
        "now": 123,
    }

    assert asyncio.run(store.expire_checkout(**expiry)) is False
    assert asyncio.run(store.expire_checkout(**expiry)) is True
    assert asyncio.run(store.checkout_status(expiry["session_id"])) == "expired"
    row = asyncio.run(
        d1_database.prepare(
            "SELECT expiration_event_id, expired_at FROM checkout_sessions WHERE order_id = ?"
        ).bind(order_id).all()
    ).results[0]
    assert row == {"expiration_event_id": expiry["event_id"], "expired_at": expiry["now"]}
    events = asyncio.run(
        d1_database.prepare("SELECT event_type FROM outbox WHERE order_id = ? ORDER BY id").bind(order_id).all()
    ).results
    assert [event["event_type"] for event in events] == ["OrderCreated", "CheckoutRedirectIssued"]


def test_d1_store_rejects_an_event_id_bound_to_a_different_type(d1_database):
    store = D1OrderStore(d1_database)
    event_id = "evt_type_conflict"
    asyncio.run(
        store.record_ignored_event(
            event_id=event_id,
            event_type="customer.created",
            payload={"id": event_id, "type": "customer.created"},
            payload_sha256="sha256:ignored",
        )
    )

    with pytest.raises(SettlementInconsistency, match="different event type"):
        asyncio.run(
            store.begin_settlement_event(
                event_id=event_id,
                session_id="cs_type_conflict",
                payload={"id": event_id},
                payload_sha256="sha256:settlement",
            )
        )


def test_d1_store_ignores_a_reserialized_unknown_event(d1_database):
    store = D1OrderStore(d1_database)
    event_id = "evt_d1_ignored"
    assert (
        asyncio.run(
            store.record_ignored_event(
                event_id=event_id,
                event_type="customer.created",
                payload={"id": event_id, "type": "customer.created"},
                payload_sha256="sha256:first-delivery",
            )
        )
        is False
    )
    assert (
        asyncio.run(
            store.record_ignored_event(
                event_id=event_id,
                event_type="customer.created",
                payload={"id": event_id, "type": "customer.created", "reserialized": True},
                payload_sha256="sha256:changed",
            )
        )
        is True
    )
    row = asyncio.run(
        d1_database.prepare(
            "SELECT payload_json, payload_sha256, processing_state FROM stripe_events WHERE stripe_event_id = ?"
        )
        .bind(event_id)
        .all()
    ).results[0]
    assert row == {
        "payload_json": '{"id":"evt_d1_ignored","type":"customer.created"}',
        "payload_sha256": "sha256:first-delivery",
        "processing_state": "ignored",
    }


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


def test_d1_store_confirms_once_and_preserves_the_first_delivery_for_a_reserialized_retry(
    fixture_catalogue, d1_database
):
    store = D1OrderStore(d1_database)
    gateway = FakePaymentGateway()
    service = CheckoutService(fixture_catalogue, store, gateway)
    request = CheckoutRequest(
        "d1-settlement-request",
        fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1),),
    )
    asyncio.run(service.checkout(request))
    order_id = gateway.requests[0].order_id
    asyncio.run(
        d1_database.prepare("UPDATE checkout_sessions SET external_session_id = ? WHERE order_id = ?")
        .bind("cs_d1_settlement", order_id)
        .run()
    )
    assert asyncio.run(store.checkout_status("cs_d1_settlement")) == "pending"
    assert asyncio.run(store.checkout_status("cs_d1_unknown")) is None

    kwargs = {
        "order_id": order_id,
        "session_id": "cs_d1_settlement",
        "event_id": "evt_d1_settlement",
        "payload": {"id": "evt_d1_settlement"},
        "payload_sha256": "sha256:d1-fixture",
        "payment_status": "paid",
        "amount_total_minor": 2_500,
        "now": 123,
    }
    begin_kwargs = {key: kwargs[key] for key in ("event_id", "session_id", "payload", "payload_sha256")}
    assert asyncio.run(store.begin_settlement_event(**begin_kwargs, now=122)) is False
    changed = {
        **kwargs,
        "payload": {"id": "evt_d1_settlement", "reserialized": True},
        "payload_sha256": "sha256:changed",
    }
    changed_begin = {key: changed[key] for key in ("event_id", "session_id", "payload", "payload_sha256")}
    assert asyncio.run(store.begin_settlement_event(**changed_begin, now=123)) is False
    asyncio.run(
        store.record_settlement_rejection(
            event_id=kwargs["event_id"],
            session_id=kwargs["session_id"],
            rejection=SettlementRejection.PROJECTION,
        )
    )
    assert asyncio.run(store.confirm_settlement(**changed)) is False
    assert asyncio.run(store.confirm_settlement(**changed)) is True
    assert asyncio.run(store.checkout_status("cs_d1_settlement")) == "confirmed"
    asyncio.run(
        store.record_settlement_rejection(
            event_id=kwargs["event_id"],
            session_id=kwargs["session_id"],
            rejection=SettlementRejection.RECONCILIATION,
        )
    )
    assert asyncio.run(store.confirm_settlement(**changed)) is True

    event_query = d1_database.prepare("SELECT event_type FROM outbox WHERE order_id = ? ORDER BY id").bind(order_id)
    events = asyncio.run(event_query.all())
    assert [event["event_type"] for event in events.results] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
        "OrderConfirmed",
    ]
    event_row = asyncio.run(
        d1_database.prepare(
            "SELECT payload_json, payload_sha256, processing_state, processing_error "
            "FROM stripe_events WHERE stripe_event_id = ?"
        )
        .bind(kwargs["event_id"])
        .all()
    ).results[0]
    assert event_row == {
        "payload_json": '{"id":"evt_d1_settlement"}',
        "payload_sha256": "sha256:d1-fixture",
        "processing_state": "confirmed",
        "processing_error": None,
    }


def test_d1_store_duplicate_insert_race_recovers_a_reserialized_received_event(d1_database, monkeypatch):
    store = D1OrderStore(d1_database)
    event_id = "evt_d1_duplicate_race"
    session_id = "cs_d1_duplicate_race"
    asyncio.run(
        store.begin_settlement_event(
            event_id=event_id,
            session_id=session_id,
            payload={"id": event_id},
            payload_sha256="sha256:first-delivery",
        )
    )
    original_first = store._first
    calls = 0

    async def hide_existing_event_once(sql, *params):
        nonlocal calls
        if "FROM stripe_events" in sql and calls == 0:
            calls += 1
            return None
        return await original_first(sql, *params)

    monkeypatch.setattr(store, "_first", hide_existing_event_once)

    assert (
        asyncio.run(
            store.begin_settlement_event(
                event_id=event_id,
                session_id=session_id,
                payload={"id": event_id, "reserialized": True},
                payload_sha256="sha256:changed",
            )
        )
        is False
    )
    row = asyncio.run(
        d1_database.prepare(
            "SELECT payload_json, payload_sha256, processing_state FROM stripe_events WHERE stripe_event_id = ?"
        )
        .bind(event_id)
        .all()
    ).results[0]
    assert row == {
        "payload_json": '{"id":"evt_d1_duplicate_race"}',
        "payload_sha256": "sha256:first-delivery",
        "processing_state": "received",
    }


def test_d1_store_confirmation_race_recovers_when_matching_event_is_confirmed(
    fixture_catalogue, d1_database, monkeypatch
):
    store, order_id, kwargs = _pending_settlement(
        fixture_catalogue,
        d1_database,
        request_id="d1-confirmation-race",
        session_id="cs_d1_confirmation_race",
        event_id="evt_d1_confirmation_race",
    )
    begin_kwargs = {key: kwargs[key] for key in ("event_id", "session_id", "payload", "payload_sha256")}
    assert asyncio.run(store.begin_settlement_event(**begin_kwargs)) is False
    original_first = store._first
    original_batch = d1_database.batch
    calls = 0

    async def hide_existing_event_once(sql, *params):
        nonlocal calls
        if "FROM stripe_events" in sql and calls == 0:
            calls += 1
            return None
        return await original_first(sql, *params)

    async def confirm_winner_then_run_batch(statements):
        await (
            d1_database.prepare("UPDATE stripe_events SET processing_state = 'confirmed' WHERE stripe_event_id = ?")
            .bind(kwargs["event_id"])
            .run()
        )
        await (
            d1_database.prepare(
                "UPDATE checkout_sessions SET settlement_state = 'confirmed', settlement_session_id = ?, "
                "settlement_event_id = ? WHERE order_id = ?"
            )
            .bind(kwargs["session_id"], kwargs["event_id"], order_id)
            .run()
        )
        await (
            d1_database.prepare(
                "INSERT INTO outbox(order_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)"
            )
            .bind(order_id, "OrderConfirmed", '{"order_id":"race"}', kwargs["now"])
            .run()
        )
        return await original_batch(statements)

    monkeypatch.setattr(store, "_first", hide_existing_event_once)
    monkeypatch.setattr(d1_database, "batch", confirm_winner_then_run_batch)

    changed = {
        **kwargs,
        "payload": {"id": kwargs["event_id"], "reserialized": True},
        "payload_sha256": "sha256:changed",
    }
    assert asyncio.run(store.confirm_settlement(**changed)) is True


def test_d1_store_confirmation_race_rejects_a_winner_with_a_different_session(
    fixture_catalogue, d1_database, monkeypatch
):
    store, _order_id, kwargs = _pending_settlement(
        fixture_catalogue,
        d1_database,
        request_id="d1-confirmation-race-conflict",
        session_id="cs_d1_confirmation_original",
        event_id="evt_d1_confirmation_conflict",
    )
    asyncio.run(
        store.begin_settlement_event(
            event_id=kwargs["event_id"],
            session_id="cs_d1_confirmation_winner",
            payload={"id": kwargs["event_id"]},
            payload_sha256="sha256:winner",
        )
    )
    original_first = store._first
    calls = 0

    async def hide_existing_event_once(sql, *params):
        nonlocal calls
        if "FROM stripe_events" in sql and calls == 0:
            calls += 1
            return None
        return await original_first(sql, *params)

    monkeypatch.setattr(store, "_first", hide_existing_event_once)

    with pytest.raises(SettlementInconsistency, match="different Checkout Session"):
        asyncio.run(store.confirm_settlement(**kwargs))
