from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import SettlementInconsistency
from .settlement import StripeSettlementService, _is_settled
from .store import ReconciliationCandidate
from .stripe import ProjectionSealError, StripeApiError

RECONCILIATION_GRACE_SECONDS = 3_600
RECONCILIATION_LEASE_SECONDS = 300
RECONCILIATION_BATCH_LIMIT = 5
RECONCILIATION_RETRY_BASE_SECONDS = 3_600
RECONCILIATION_RETRY_MAX_SECONDS = 86_400


@dataclass(frozen=True)
class ReconciliationRun:
    claimed: int
    confirmed: int
    expired: int
    deferred: int
    failures: int


@dataclass(frozen=True)
class StripeReconciliationService:
    store: object
    retriever: object
    projection_verifier: object

    async def reconcile(self, *, now: int | None = None) -> ReconciliationRun:
        now = int(time.time()) if now is None else now
        candidates = await self.store.claim_reconciliation_candidates(
            now=now,
            stale_before=now - RECONCILIATION_GRACE_SECONDS,
            limit=RECONCILIATION_BATCH_LIMIT,
            lease_seconds=RECONCILIATION_LEASE_SECONDS,
        )
        confirmed = expired = deferred = failures = 0
        for candidate in candidates:
            outcome = await self._reconcile_candidate(candidate, now=now)
            if outcome == "confirmed":
                confirmed += 1
            elif outcome == "expired":
                expired += 1
            elif outcome == "deferred":
                deferred += 1
            else:
                failures += 1
        return ReconciliationRun(len(candidates), confirmed, expired, deferred, failures)

    async def _reconcile_candidate(self, candidate: ReconciliationCandidate, *, now: int) -> str:
        try:
            session = await self.retriever.retrieve(candidate.session_id)
        except StripeApiError:
            await self._retry(candidate, now=now, outcome="transient_error", error="stripe_retrieval_failed")
            return "failure"
        except Exception:
            await self._retry(candidate, now=now, outcome="transient_error", error="stripe_retrieval_failed")
            return "failure"

        try:
            if session.livemode or session.mode != "payment" or session.session_id != candidate.session_id:
                raise SettlementInconsistency("Stripe Checkout Session identity is inconsistent")
            projection_items = await self.projection_verifier.verify(
                session_id=session.session_id, metadata=session.metadata
            )
            order_id = session.metadata.get("cr_order_id")
            if order_id != candidate.order_id:
                raise SettlementInconsistency("CartRay projection has an inconsistent order ID")
            context = await self.store.settlement_context(order_id)
            if context.settlement_state != "pending":
                return "deferred"
            StripeSettlementService._reconcile(context, session, projection_items)
        except ProjectionSealError:
            await self._retry(
                candidate,
                now=now,
                outcome="rejected",
                error="projection_rejected",
                session=session,
            )
            return "failure"
        except SettlementInconsistency:
            await self._retry(
                candidate,
                now=now,
                outcome="rejected",
                error="reconciliation_rejected",
                session=session,
            )
            return "failure"
        except Exception:
            await self._retry(
                candidate,
                now=now,
                outcome="transient_error",
                error="reconciliation_lookup_failed",
                session=session,
            )
            return "failure"

        if session.status == "open":
            await self._retry(candidate, now=now, outcome="open", error=None, session=session)
            return "deferred"
        if session.status == "expired":
            try:
                duplicate = await self.store.expire_reconciliation(
                    candidate=candidate,
                    now=now,
                    payment_status=session.payment_status,
                    amount_total_minor=session.amount_total_minor,
                )
            except SettlementInconsistency:
                return "deferred"
            return "deferred" if duplicate else "expired"
        if session.status == "complete":
            if not _is_settled(session.amount_total_minor, session.status, session.payment_status):
                await self._retry(
                    candidate,
                    now=now,
                    outcome="unsettled",
                    error="checkout_not_settled",
                    session=session,
                )
                return "deferred"
            try:
                duplicate = await self.store.confirm_reconciliation(
                    candidate=candidate,
                    payment_status=session.payment_status,
                    amount_total_minor=session.amount_total_minor,
                    now=now,
                    items_digest=context.order.items_digest,
                )
            except SettlementInconsistency:
                return "deferred"
            return "deferred" if duplicate else "confirmed"
        await self._retry(
            candidate,
            now=now,
            outcome="rejected",
            error="unrecognized_checkout_state",
            session=session,
        )
        return "failure"

    async def _retry(
        self,
        candidate: ReconciliationCandidate,
        *,
        now: int,
        outcome: str,
        error: str | None,
        session=None,
    ) -> None:
        await self.store.finish_reconciliation_retry(
            candidate=candidate,
            now=now,
            next_attempt_at=now + _retry_delay(candidate.attempt_count),
            outcome=outcome,
            error=error,
            observed_status=None if session is None else session.status,
            observed_payment_status=None if session is None else session.payment_status,
            observed_amount_total_minor=None if session is None else session.amount_total_minor,
        )


def _retry_delay(attempt_count: int) -> int:
    exponent = min(max(attempt_count - 1, 0), 4)
    return min(RECONCILIATION_RETRY_BASE_SECONDS * (2**exponent), RECONCILIATION_RETRY_MAX_SECONDS)
