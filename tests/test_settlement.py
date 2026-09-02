from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from cartray.canonical import CanonicalItem, parse_projection_items
from cartray.errors import SettlementInconsistency, WebhookValidationError
from cartray.models import CheckoutRequest
from cartray.settlement import StripeSettlementService
from cartray.store import SettlementRejection
from cartray.stripe import ProjectionSealError, StripeCheckoutSession, StripeLineItem
from cartray.webhook import StripeWebhookEvent


@dataclass
class StaticRetriever:
    session: StripeCheckoutSession

    async def retrieve(self, session_id: str) -> StripeCheckoutSession:
        assert session_id == self.session.session_id
        return self.session


class FailingRetriever:
    async def retrieve(self, _session_id: str):
        raise AssertionError("a confirmed Stripe event must not retrieve Stripe again")


@dataclass
class FailOnceRetriever:
    session: StripeCheckoutSession
    attempts: int = 0

    async def retrieve(self, session_id: str) -> StripeCheckoutSession:
        assert session_id == self.session.session_id
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary Stripe failure")
        return self.session


@dataclass
class FixtureProjectionVerifier:
    async def verify(self, *, session_id: str, metadata: dict[str, str]):
        assert session_id.startswith("cs_")
        return parse_projection_items(metadata)


class RejectingProjectionVerifier:
    async def verify(self, *, session_id: str, metadata: dict[str, str]):
        raise ProjectionSealError("fixture projection rejection")


class DiagnosticWriteFailureStore:
    def __init__(self, store) -> None:
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    async def record_settlement_rejection(self, **_kwargs) -> None:
        raise RuntimeError("diagnostic persistence failed")


def _checkout_session(service, gateway, request, *, payment_status: str, amount_total_minor: int):
    asyncio.run(service.checkout(request))
    spec = gateway.requests[-1]
    session_id = f"cs_settlement_{len(gateway.requests):06d}"
    service.store.connection.execute(
        "UPDATE checkout_sessions SET external_session_id = ? WHERE order_id = ?", (session_id, spec.order_id)
    )
    lines = tuple(
        StripeLineItem(
            f"li_{index}",
            price_id,
            quantity,
            next(
                item["unit_amount_minor"]
                for item in service.store.order_items(spec.order_id)
                if item["stripe_price_id"] == price_id
            ),
            "aud",
        )
        for index, (price_id, quantity) in enumerate(spec.line_items, start=1)
    )
    session = StripeCheckoutSession(
        session_id=session_id,
        livemode=False,
        mode="payment",
        status="complete",
        payment_status=payment_status,
        amount_total_minor=amount_total_minor,
        currency="aud",
        metadata=spec.metadata,
        line_items=lines,
    )
    event = StripeWebhookEvent(
        event_id=f"evt_settlement_{len(gateway.requests):06d}",
        event_type="checkout.session.completed",
        livemode=False,
        session_id=session_id,
        payload={"id": f"evt_settlement_{len(gateway.requests):06d}"},
        payload_sha256=f"sha256:fixture-{len(gateway.requests):06d}",
    )
    return spec, session, event


def _service(store, session):
    return StripeSettlementService(store, StaticRetriever(session), FixtureProjectionVerifier())


def _processing_error(store, event: StripeWebhookEvent) -> str | None:
    return store.connection.execute(
        "SELECT processing_error FROM stripe_events WHERE stripe_event_id = ?", (event.event_id,)
    ).fetchone()["processing_error"]


def test_paid_settlement_confirms_a_quantity_five_order_once(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest(
            "settlement-support-hours", checkout.catalogue.version, (CanonicalItem("TEST-SUPPORT-HOURS", 5),)
        ),
        payment_status="paid",
        amount_total_minor=50_000,
    )
    service = _service(checkout.store, session)

    assert asyncio.run(service.settle(event)) is False
    assert asyncio.run(StripeSettlementService(checkout.store, FailingRetriever(), None).settle(event)) is True
    distinct_event = StripeWebhookEvent(
        **{**event.__dict__, "event_id": "evt_settlement_distinct", "payload_sha256": "sha256:distinct"}
    )
    assert asyncio.run(service.settle(distinct_event)) is True

    settlement = checkout.store.connection.execute(
        "SELECT settlement_state, settlement_session_id, stripe_payment_status, stripe_amount_total_minor "
        "FROM checkout_sessions WHERE order_id = ?",
        (spec.order_id,),
    ).fetchone()
    assert dict(settlement) == {
        "settlement_state": "confirmed",
        "settlement_session_id": session.session_id,
        "stripe_payment_status": "paid",
        "stripe_amount_total_minor": 50_000,
    }
    assert [event["event_type"] for event in checkout.store.outbox_events(spec.order_id)] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
        "OrderConfirmed",
    ]


@pytest.mark.parametrize(
    ("amount_total_minor", "status", "payment_status"),
    (
        (5_000, "complete", "unpaid"),
        (0, "complete", "unpaid"),
        (5_000, "complete", "no_payment_required"),
        (0, "open", "paid"),
    ),
)
def test_settlement_rejects_unsettled_checkout_sessions(checkout_service, amount_total_minor, status, payment_status):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-predicate", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status=payment_status,
        amount_total_minor=amount_total_minor,
    )
    with pytest.raises(SettlementInconsistency, match="not settled"):
        unsettled_session = StripeCheckoutSession(**{**session.__dict__, "status": status})
        asyncio.run(_service(checkout.store, unsettled_session).settle(event))
    assert _processing_error(checkout.store, event) == SettlementRejection.PREDICATE


def test_session_rejection_records_a_fixed_operator_diagnostic(checkout_service):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-session", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )

    with pytest.raises(SettlementInconsistency, match="identity"):
        asyncio.run(
            _service(checkout.store, StripeCheckoutSession(**{**session.__dict__, "livemode": True})).settle(event)
        )
    assert _processing_error(checkout.store, event) == SettlementRejection.SESSION


def test_diagnostic_write_failure_does_not_replace_a_settlement_rejection(checkout_service):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest(
            "settlement-diagnostic-write", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)
        ),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    service = _service(
        DiagnosticWriteFailureStore(checkout.store), StripeCheckoutSession(**{**session.__dict__, "livemode": True})
    )

    with pytest.raises(SettlementInconsistency, match="identity"):
        asyncio.run(service.settle(event))


def test_projection_rejection_records_a_fixed_operator_diagnostic(checkout_service):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-projection", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    service = StripeSettlementService(checkout.store, StaticRetriever(session), RejectingProjectionVerifier())

    with pytest.raises(ProjectionSealError, match="fixture"):
        asyncio.run(service.settle(event))
    assert _processing_error(checkout.store, event) == SettlementRejection.PROJECTION


def test_order_context_rejection_records_a_fixed_operator_diagnostic(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-context", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    checkout.store.connection.execute(
        "UPDATE checkout_sessions SET external_session_id = NULL WHERE order_id = ?", (spec.order_id,)
    )

    with pytest.raises(SettlementInconsistency, match="no Checkout Session"):
        asyncio.run(_service(checkout.store, session).settle(event))
    assert _processing_error(checkout.store, event) == SettlementRejection.ORDER_CONTEXT


def test_reconciliation_rejection_records_a_fixed_operator_diagnostic(checkout_service):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-reconciliation", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    changed_nonce = StripeCheckoutSession(**{**session.__dict__, "metadata": {**session.metadata, "cr_nonce": "other"}})

    with pytest.raises(SettlementInconsistency, match="projection"):
        asyncio.run(_service(checkout.store, changed_nonce).settle(event))
    assert _processing_error(checkout.store, event) == SettlementRejection.RECONCILIATION


def test_confirmation_rejection_records_a_fixed_operator_diagnostic(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-confirmation", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    checkout.store.connection.execute(
        "UPDATE checkout_sessions SET settlement_session_id = ? WHERE order_id = ?",
        ("cs_settlement_other", spec.order_id),
    )

    with pytest.raises(SettlementInconsistency, match="different Checkout Session"):
        asyncio.run(_service(checkout.store, session).settle(event))
    assert _processing_error(checkout.store, event) == SettlementRejection.CONFIRMATION


def test_settlement_rejects_a_different_session(checkout_service):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-predicate", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )

    different_session = StripeCheckoutSession(**{**session.__dict__, "session_id": "cs_settlement_other"})
    different_event = StripeWebhookEvent(
        **{
            **event.__dict__,
            "event_id": "evt_settlement_other",
            "session_id": different_session.session_id,
            "payload_sha256": "sha256:other",
        }
    )
    with pytest.raises(SettlementInconsistency, match="different Checkout Session"):
        asyncio.run(_service(checkout.store, different_session).settle(different_event))


def test_transient_retrieval_failure_resumes_the_same_received_event(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-retry", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    retriever = FailOnceRetriever(session)
    service = StripeSettlementService(checkout.store, retriever, FixtureProjectionVerifier())

    with pytest.raises(RuntimeError, match="temporary Stripe"):
        asyncio.run(service.settle(event))
    processing_row = checkout.store.connection.execute(
        "SELECT processing_state, processing_error FROM stripe_events WHERE stripe_event_id = ?", (event.event_id,)
    ).fetchone()
    assert dict(processing_row) == {"processing_state": "received", "processing_error": None}

    assert asyncio.run(service.settle(event)) is False
    assert retriever.attempts == 2
    assert [row["event_type"] for row in checkout.store.outbox_events(spec.order_id)] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
        "OrderConfirmed",
    ]


def test_exact_retry_clears_a_rejection_diagnostic_when_it_confirms(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest(
            "settlement-retry-diagnostic", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)
        ),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    unpaid = StripeCheckoutSession(**{**session.__dict__, "payment_status": "unpaid"})

    with pytest.raises(SettlementInconsistency, match="not settled"):
        asyncio.run(_service(checkout.store, unpaid).settle(event))
    assert _processing_error(checkout.store, event) == SettlementRejection.PREDICATE

    assert asyncio.run(_service(checkout.store, session).settle(event)) is False
    assert _processing_error(checkout.store, event) is None
    assert [row["event_type"] for row in checkout.store.outbox_events(spec.order_id)] == [
        "OrderCreated",
        "CheckoutRedirectIssued",
        "OrderConfirmed",
    ]
    asyncio.run(
        checkout.store.record_settlement_rejection(
            event_id=event.event_id,
            session_id=session.session_id,
            payload_sha256=event.payload_sha256,
            rejection=SettlementRejection.PROJECTION,
        )
    )
    assert _processing_error(checkout.store, event) is None


@pytest.mark.parametrize(
    ("mutation", "projection_items", "message"),
    [
        (
            lambda session: {**session.__dict__, "metadata": {**session.metadata, "cr_nonce": "other"}},
            None,
            "projection",
        ),
        (
            lambda session: {
                **session.__dict__,
                "metadata": {**session.metadata, "cr_catalogue_version": "sha256:other"},
            },
            None,
            "projection",
        ),
        (
            lambda session: {**session.__dict__, "metadata": {**session.metadata, "cr_items_digest": "sha256:other"}},
            None,
            "projection",
        ),
        (lambda session: {**session.__dict__, "currency": "usd"}, None, "currency"),
        (lambda session: dict(session.__dict__), (CanonicalItem("TEST-FREE", 1),), "items"),
    ],
)
def test_reconciliation_rejects_every_remaining_immutable_projection_mismatch(
    checkout_service, mutation, projection_items, message
):
    checkout, gateway = checkout_service
    spec, session, _event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-reconcile", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    context = asyncio.run(checkout.store.settlement_context(spec.order_id))
    mutated_session = StripeCheckoutSession(**mutation(session))
    actual_items = parse_projection_items(session.metadata) if projection_items is None else projection_items

    with pytest.raises(SettlementInconsistency, match=message):
        StripeSettlementService._reconcile(context, mutated_session, actual_items)


def test_free_order_is_confirmed_from_checkout_completion_alone(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-free", checkout.catalogue.version, (CanonicalItem("TEST-FREE", 1),)),
        payment_status="paid",
        amount_total_minor=0,
    )

    assert asyncio.run(_service(checkout.store, session).settle(event)) is False
    assert checkout.store.outbox_events(spec.order_id)[-1]["event_type"] == "OrderConfirmed"


def test_discounted_paid_catalogue_order_can_settle_at_zero(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-discounted", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="no_payment_required",
        amount_total_minor=0,
    )

    assert asyncio.run(_service(checkout.store, session).settle(event)) is False
    assert checkout.store.outbox_events(spec.order_id)[-1]["event_type"] == "OrderConfirmed"


def test_settlement_rejects_modified_stripe_lines_without_persisting_an_event(checkout_service):
    checkout, gateway = checkout_service
    spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-mismatch", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    bad_session = StripeCheckoutSession(
        **{**session.__dict__, "line_items": (StripeLineItem("li_bad", "price_fixture_template", 2, 5_000, "aud"),)}
    )

    with pytest.raises(SettlementInconsistency, match="line items"):
        asyncio.run(_service(checkout.store, bad_session).settle(event))

    assert (
        checkout.store.connection.execute(
            "SELECT settlement_state FROM checkout_sessions WHERE order_id = ?", (spec.order_id,)
        ).fetchone()["settlement_state"]
        == "pending"
    )
    event_row = checkout.store.connection.execute(
        "SELECT processing_state FROM stripe_events WHERE stripe_event_id = ?", (event.event_id,)
    ).fetchone()
    assert event_row["processing_state"] == "received"


def test_same_event_id_with_a_different_payload_hash_is_an_inconsistency(checkout_service):
    checkout, gateway = checkout_service
    _spec, session, event = _checkout_session(
        checkout,
        gateway,
        CheckoutRequest("settlement-event-hash", checkout.catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
        payment_status="paid",
        amount_total_minor=5_000,
    )
    service = _service(checkout.store, session)
    assert asyncio.run(service.settle(event)) is False
    changed = StripeWebhookEvent(**{**event.__dict__, "payload_sha256": "sha256:changed"})

    with pytest.raises(SettlementInconsistency, match="payload hash"):
        asyncio.run(service.settle(changed))


def test_verified_unknown_event_is_recorded_as_ignored(checkout_service):
    checkout, _gateway = checkout_service
    event = StripeWebhookEvent(
        event_id="evt_ignored",
        event_type="customer.created",
        livemode=False,
        session_id=None,
        payload={"id": "evt_ignored", "type": "customer.created"},
        payload_sha256="sha256:ignored",
    )
    service = StripeSettlementService(checkout.store, None, None)

    assert asyncio.run(service.settle(event)) is False
    assert asyncio.run(service.settle(event)) is True
    row = checkout.store.connection.execute(
        "SELECT event_type, processing_state FROM stripe_events WHERE stripe_event_id = ?", (event.event_id,)
    ).fetchone()
    assert dict(row) == {"event_type": "customer.created", "processing_state": "ignored"}


def test_live_mode_unknown_event_is_rejected_before_it_can_enter_the_ledger():
    event = StripeWebhookEvent(
        event_id="evt_live_unknown",
        event_type="customer.created",
        livemode=True,
        session_id=None,
        payload={"id": "evt_live_unknown"},
        payload_sha256="sha256:live",
    )

    with pytest.raises(WebhookValidationError, match="environment"):
        asyncio.run(StripeSettlementService(None, None, None).settle(event))
