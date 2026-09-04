from __future__ import annotations

import asyncio
from dataclasses import dataclass

from cartray.canonical import CanonicalItem, parse_projection_items
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRequest
from cartray.reconciliation import (
    RECONCILIATION_BATCH_LIMIT,
    RECONCILIATION_GRACE_SECONDS,
    StripeReconciliationService,
)
from cartray.settlement import StripeSettlementService
from cartray.store import SqliteOrderStore
from cartray.stripe import StripeCheckoutSession, StripeLineItem
from cartray.webhook import StripeWebhookEvent


@dataclass
class StaticRetriever:
    sessions: dict[str, StripeCheckoutSession]

    async def retrieve(self, session_id: str) -> StripeCheckoutSession:
        return self.sessions[session_id]


class FixtureProjectionVerifier:
    async def verify(self, *, session_id: str, metadata: dict[str, str]):
        assert session_id.startswith("cs_")
        return parse_projection_items(metadata)


def _pending_checkout(
    fixture_catalogue,
    *,
    request_id: str,
    product_key: str = "TEST-TEMPLATE",
    quantity: int = 1,
    status: str = "complete",
    payment_status: str = "paid",
):
    store = SqliteOrderStore.in_memory()
    gateway = FakePaymentGateway()
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    asyncio.run(
        checkout.checkout(
            CheckoutRequest(request_id, fixture_catalogue.version, (CanonicalItem(product_key, quantity),))
        )
    )
    spec = gateway.requests[-1]
    session_id = f"cs_reconcile_{request_id}"
    store.connection.execute(
        "UPDATE checkout_sessions SET external_session_id = ?, updated_at = 0 WHERE order_id = ?",
        (session_id, spec.order_id),
    )
    order = asyncio.run(store.load_order(spec.order_id))
    lines = tuple(
        StripeLineItem(f"li_{index}", item.stripe_price_id, item.quantity, item.unit_amount_minor, order.currency)
        for index, item in enumerate(order.items, start=1)
    )
    session = StripeCheckoutSession(
        session_id=session_id,
        livemode=False,
        mode="payment",
        status=status,
        payment_status=payment_status,
        amount_total_minor=order.subtotal_minor,
        currency=order.currency,
        metadata=spec.metadata,
        line_items=lines,
    )
    return store, spec, session


def _reconciler(store, session):
    return StripeReconciliationService(
        store, StaticRetriever({session.session_id: session}), FixtureProjectionVerifier()
    )


def _run(store, session, *, now: int = 10_000):
    return asyncio.run(_reconciler(store, session).reconcile(now=now))


def test_reconciliation_confirms_paid_session_without_fabricating_a_stripe_event(fixture_catalogue):
    store, spec, session = _pending_checkout(fixture_catalogue, request_id="paid")

    result = _run(store, session)

    assert result.confirmed == 1
    checkout = store.connection.execute(
        "SELECT settlement_state, settlement_event_id, stripe_payment_status, stripe_amount_total_minor "
        "FROM checkout_sessions WHERE order_id = ?",
        (spec.order_id,),
    ).fetchone()
    assert dict(checkout) == {
        "settlement_state": "confirmed",
        "settlement_event_id": None,
        "stripe_payment_status": "paid",
        "stripe_amount_total_minor": 2_500,
    }
    assert store.connection.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 0
    assert [row["event_type"] for row in store.outbox_events(spec.order_id)] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
        "OrderConfirmed",
    ]
    audit = store.connection.execute(
        "SELECT attempt_count, last_outcome, observed_status FROM checkout_reconciliations WHERE order_id = ?",
        (spec.order_id,),
    ).fetchone()
    assert dict(audit) == {"attempt_count": 1, "last_outcome": "confirmed", "observed_status": "complete"}


def test_reconciliation_accepts_native_free_no_payment_required_once(fixture_catalogue):
    store, spec, session = _pending_checkout(
        fixture_catalogue,
        request_id="free",
        product_key="TEST-FREE",
        payment_status="no_payment_required",
    )

    assert _run(store, session).confirmed == 1
    assert dict(
        store.connection.execute(
            "SELECT settlement_state, stripe_amount_total_minor FROM checkout_sessions WHERE order_id = ?",
            (spec.order_id,),
        ).fetchone()
    ) == {"settlement_state": "confirmed", "stripe_amount_total_minor": 0}
    assert [row["event_type"] for row in store.outbox_events(spec.order_id)].count("OrderConfirmed") == 1


def test_late_completed_webhook_after_reconciliation_is_ledgered_without_a_second_confirmation(fixture_catalogue):
    store, spec, session = _pending_checkout(fixture_catalogue, request_id="late-completion")

    assert _run(store, session).confirmed == 1
    event = StripeWebhookEvent(
        event_id="evt_late_completion",
        event_type="checkout.session.completed",
        livemode=False,
        session_id=session.session_id,
        payload={"id": "evt_late_completion"},
        payload_sha256="sha256:late-completion",
    )

    assert (
        asyncio.run(
            StripeSettlementService(
                store, StaticRetriever({session.session_id: session}), FixtureProjectionVerifier()
            ).settle(event)
        )
        is True
    )
    assert store.connection.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 1
    assert [row["event_type"] for row in store.outbox_events(spec.order_id)].count("OrderConfirmed") == 1


def test_reconciliation_expires_without_confirmation_and_late_webhook_is_harmless(fixture_catalogue):
    store, spec, session = _pending_checkout(
        fixture_catalogue, request_id="expired", product_key="TEST-FREE", status="expired", payment_status="unpaid"
    )

    assert _run(store, session).expired == 1
    assert dict(
        store.connection.execute(
            "SELECT settlement_state, expiration_event_id FROM checkout_sessions WHERE order_id = ?",
            (spec.order_id,),
        ).fetchone()
    ) == {"settlement_state": "expired", "expiration_event_id": None}
    assert [row["event_type"] for row in store.outbox_events(spec.order_id)] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
    ]

    event = StripeWebhookEvent(
        event_id="evt_late_expiry",
        event_type="checkout.session.expired",
        livemode=False,
        session_id=session.session_id,
        payload={"id": "evt_late_expiry"},
        payload_sha256="sha256:late-expiry",
    )
    assert asyncio.run(StripeSettlementService(store, None, None).settle(event)) is True
    assert store.connection.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 1


def test_open_and_unsettled_sessions_remain_pending_with_backoff(fixture_catalogue):
    store, spec, session = _pending_checkout(fixture_catalogue, request_id="open", status="open")

    assert _run(store, session).deferred == 1
    audit = store.connection.execute(
        "SELECT attempt_count, next_attempt_at, last_outcome FROM checkout_reconciliations WHERE order_id = ?",
        (spec.order_id,),
    ).fetchone()
    assert dict(audit) == {"attempt_count": 1, "next_attempt_at": 13_600, "last_outcome": "open"}
    assert _run(store, session, now=10_001).claimed == 0

    store2, spec2, unpaid = _pending_checkout(fixture_catalogue, request_id="unpaid", payment_status="unpaid")
    assert _run(store2, unpaid).deferred == 1
    assert (
        store2.connection.execute(
            "SELECT settlement_state FROM checkout_sessions WHERE order_id = ?", (spec2.order_id,)
        ).fetchone()[0]
        == "pending"
    )


def test_reconciliation_rejects_projection_mismatch_without_terminal_transition(fixture_catalogue):
    store, spec, session = _pending_checkout(fixture_catalogue, request_id="projection")
    invalid = StripeCheckoutSession(**{**session.__dict__, "metadata": {**session.metadata, "cr_nonce": "wrong"}})

    assert _run(store, invalid).failures == 1
    assert (
        store.connection.execute(
            "SELECT settlement_state FROM checkout_sessions WHERE order_id = ?", (spec.order_id,)
        ).fetchone()[0]
        == "pending"
    )
    assert dict(
        store.connection.execute(
            "SELECT last_outcome, last_error FROM checkout_reconciliations WHERE order_id = ?", (spec.order_id,)
        ).fetchone()
    ) == {"last_outcome": "rejected", "last_error": "reconciliation_rejected"}
    assert store.connection.execute("SELECT count(*) FROM stripe_events").fetchone()[0] == 0


def test_reconciliation_claims_at_most_the_fixed_deterministic_batch(fixture_catalogue):
    store = SqliteOrderStore.in_memory()
    gateway = FakePaymentGateway()
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    for index in range(RECONCILIATION_BATCH_LIMIT + 1):
        asyncio.run(
            checkout.checkout(
                CheckoutRequest(f"batch-{index}", fixture_catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),))
            )
        )
        spec = gateway.requests[-1]
        store.connection.execute(
            "UPDATE checkout_sessions SET external_session_id = ?, updated_at = 0 WHERE order_id = ?",
            (f"cs_reconcile_batch_{index}", spec.order_id),
        )

    candidates = asyncio.run(
        store.claim_reconciliation_candidates(
            now=RECONCILIATION_GRACE_SECONDS + 1,
            stale_before=1,
            limit=RECONCILIATION_BATCH_LIMIT,
            lease_seconds=300,
        )
    )

    assert len(candidates) == RECONCILIATION_BATCH_LIMIT
    assert [candidate.order_id for candidate in candidates] == sorted(candidate.order_id for candidate in candidates)
