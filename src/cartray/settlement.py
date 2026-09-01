from __future__ import annotations

from dataclasses import dataclass

from .canonical import CanonicalItem, canonical_items
from .errors import SettlementInconsistency, WebhookValidationError
from .store import SettlementContext
from .stripe import CheckoutMetadataVerifier, StripeCheckoutSessionRetriever
from .webhook import StripeWebhookEvent


@dataclass(frozen=True)
class StripeSettlementService:
    store: object
    retriever: StripeCheckoutSessionRetriever
    projection_verifier: CheckoutMetadataVerifier

    async def settle(self, event: StripeWebhookEvent) -> bool:
        if event.livemode:
            raise WebhookValidationError("Stripe webhook environment or Session is invalid")
        if event.event_type != "checkout.session.completed":
            return await self.store.record_ignored_event(
                event_id=event.event_id,
                event_type=event.event_type,
                payload=event.payload,
                payload_sha256=event.payload_sha256,
            )
        if event.session_id is None:
            raise WebhookValidationError("Stripe webhook environment or Session is invalid")
        if await self.store.begin_settlement_event(
            event_id=event.event_id,
            session_id=event.session_id,
            payload=event.payload,
            payload_sha256=event.payload_sha256,
        ):
            return True
        session = await self.retriever.retrieve(event.session_id)
        if session.livemode or session.mode != "payment" or session.session_id != event.session_id:
            raise SettlementInconsistency("Stripe Checkout Session identity is inconsistent")
        projection_items = await self.projection_verifier.verify(
            session_id=session.session_id, metadata=session.metadata
        )
        order_id = session.metadata.get("cr_order_id")
        if not isinstance(order_id, str):
            raise SettlementInconsistency("CartRay projection has no order ID")
        context: SettlementContext = await self.store.settlement_context(order_id)
        self._reconcile(context, session, projection_items)
        if not _is_settled(session.amount_total_minor, session.status, session.payment_status):
            raise SettlementInconsistency("Stripe Checkout Session is not settled")
        return await self.store.confirm_settlement(
            order_id=order_id,
            session_id=session.session_id,
            event_id=event.event_id,
            payload=event.payload,
            payload_sha256=event.payload_sha256,
            payment_status=session.payment_status,
            amount_total_minor=session.amount_total_minor,
        )

    @staticmethod
    def _reconcile(context, session, projection_items) -> None:
        order = context.order
        if context.checkout_session_id != session.session_id:
            raise SettlementInconsistency("D1 order is bound to a different Checkout Session")
        metadata = session.metadata
        if (
            metadata.get("cr_nonce") != context.nonce
            or metadata.get("cr_catalogue_version") != order.manifest_version
            or metadata.get("cr_items_digest") != order.items_digest
        ):
            raise SettlementInconsistency("CartRay projection does not match its immutable order")
        expected_items = canonical_items(CanonicalItem(item.product_key, item.quantity) for item in order.items)
        if projection_items != expected_items:
            raise SettlementInconsistency("CartRay projection items do not match its immutable order")
        expected_lines = sorted((item.stripe_price_id, item.quantity, item.unit_amount_minor) for item in order.items)
        actual_lines = sorted((item.price_id, item.quantity, item.unit_amount_minor) for item in session.line_items)
        if expected_lines != actual_lines or any(item.currency != order.currency for item in session.line_items):
            raise SettlementInconsistency("Stripe line items do not match the immutable order")
        if session.currency != order.currency:
            raise SettlementInconsistency("Stripe Checkout Session currency does not match the immutable order")


def _is_settled(amount_total_minor: int, status: str, payment_status: str) -> bool:
    return status == "complete" and (
        (amount_total_minor > 0 and payment_status == "paid")
        or (amount_total_minor == 0 and payment_status == "no_payment_required")
    )
