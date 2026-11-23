"""Bounded post-purchase actions over confirmed commerce outbox records.

Handlers are trusted server-side implementations. A claimed external side effect
is never automatically retried: an unknown outcome requires operator review.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class Purchase:
    order_id: str
    session_id: str
    items: tuple[tuple[str, int], ...]


class _ReceiptStore:
    """Small persistence helpers shared by the receipt drain and its recovery."""

    def __init__(self, database, merchant: str, environment: str, handlers: Mapping[str, Callable]):
        if not _IDENTIFIER.fullmatch(merchant) or not _IDENTIFIER.fullmatch(environment):
            raise ValueError("invalid merchant identity")
        self.database, self.merchant, self.environment, self.handlers = database, merchant, environment, dict(handlers)

    async def _first(self, sql, *values):
        from cartray.store import _results

        result = await self.database.prepare(sql).bind(*values).all()
        rows = _results(result)
        return rows[0] if rows else None

    async def _hold_after_cancellation(self, order_id, workflow_key):
        try:
            await asyncio.wait_for(self._mark_uncertain(order_id, workflow_key), timeout=1)
        except Exception:
            pass  # A remaining claimed row is resolved by the operator.

    async def _mark_uncertain(self, order_id, workflow_key):
        await (
            self.database.prepare(
                """UPDATE post_purchase_executions SET state='uncertain',updated_at=?
                WHERE merchant_id=? AND environment=? AND order_id=? AND workflow_key=? AND state='claimed'"""
            )
            .bind(int(time.time()), self.merchant, self.environment, order_id, workflow_key)
            .run()
        )


async def drain_confirmed_d1(
    dispatcher: _ReceiptStore,
    *,
    limit: int = 10,
    max_new_claims: int = 2,
    deadline: float | None = None,
) -> int:
    """Consume a bounded batch from an explicitly activated independent checkpoint."""
    from cartray.store import _changes, _results

    if not 1 <= limit <= 50 or not 1 <= max_new_claims <= 10:
        raise ValueError("invalid post-purchase batch limit")
    checkpoint = await dispatcher._first(
        """SELECT merchant_id,environment,activation_watermark,last_outbox_id
        FROM post_purchase_checkpoint WHERE singleton=1"""
    )
    if (
        not checkpoint
        or (checkpoint["merchant_id"], checkpoint["environment"]) != (dispatcher.merchant, dispatcher.environment)
        or checkpoint["last_outbox_id"] < checkpoint["activation_watermark"]
    ):
        raise ValueError("post-purchase checkpoint is missing or mismatched")
    previous = checkpoint["last_outbox_id"]
    result = (
        await dispatcher.database.prepare(
            """SELECT e.id,e.order_id,e.payload_json,s.external_session_id,o.items_digest
        FROM outbox e JOIN orders o USING(order_id) JOIN checkout_sessions s USING(order_id)
        WHERE e.event_type='OrderConfirmed' AND e.id>? ORDER BY e.id LIMIT ?"""
        )
        .bind(previous, limit)
        .all()
    )
    processed = 0
    remaining_claims = max_new_claims
    for event in _results(result):
        payload = json.loads(event["payload_json"])
        if (
            not isinstance(payload, dict)
            or payload.get("order_id") != event["order_id"]
            or payload.get("session_id") != event["external_session_id"]
            or payload.get("items_digest") != event["items_digest"]
        ):
            print(f"Post-purchase event {event['id']}: immutable purchase mismatch")
            raise ValueError("confirmed event does not match the immutable purchase")
        try:
            outcomes = await dispatcher.dispatch(event["order_id"], max_new_claims=remaining_claims, deadline=deadline)
        except ValueError:
            print(f"Post-purchase event {event['id']}: workflow validation failed")
            raise
        remaining_claims -= sum(state in ("accepted", "uncertain") for state in outcomes.values())
        if "pending_claim" in outcomes.values() or "deferred" in outcomes.values():
            return processed
        advanced = (
            await dispatcher.database.prepare(
                """UPDATE post_purchase_checkpoint SET last_outbox_id=?
            WHERE singleton=1 AND merchant_id=? AND environment=? AND last_outbox_id=?"""
            )
            .bind(event["id"], dispatcher.merchant, dispatcher.environment, previous)
            .run()
        )
        if _changes(advanced) != 1:
            # A competing consumer may have moved the checkpoint. Stop; replay is safe.
            return processed
        previous = event["id"]
        processed += 1
    return processed


class OrderReceiptDispatcher(_ReceiptStore):
    """Deliver one confirmed-order message using the existing execution ledger.

    A legacy workflow claim holds or satisfies the order during handover. It is
    never reset or automatically resent. Deployments must stop the old sender
    and let in-flight work settle before enabling this dispatcher.
    """

    async def dispatch(
        self,
        order_id: str,
            *,
        max_new_claims: int | None = None,
        deadline: float | None = None,
    ) -> dict[str, str]:
        from inspect import isawaitable

        from cartray.store import _changes, _results

        if not isinstance(order_id, str) or not _IDENTIFIER.fullmatch(order_id):
            raise ValueError("invalid order")
        binding = await self._first("SELECT merchant_id,environment FROM post_purchase_installation WHERE singleton=1")
        if not binding or (binding["merchant_id"], binding["environment"]) != (self.merchant, self.environment):
            raise ValueError("post-purchase installation binding mismatch")
        order = await self._first(
            """SELECT s.external_session_id FROM checkout_sessions s WHERE s.order_id=?
            AND s.settlement_state='confirmed' AND EXISTS (SELECT 1 FROM outbox e
            WHERE e.order_id=s.order_id AND e.event_type='OrderConfirmed')""",
            order_id,
        )
        if not order or not order["external_session_id"]:
            raise ValueError("purchase is not confirmed")
        existing = _results(
            await self.database.prepare(
                """SELECT workflow_key,state FROM post_purchase_executions
            WHERE merchant_id=? AND environment=? AND order_id=?"""
            )
            .bind(self.merchant, self.environment, order_id)
            .all()
        )
        if existing:
            return {
                "receipt": "pending_claim" if any(row["state"] == "claimed" for row in existing) else "already_claimed"
            }
        if (max_new_claims is not None and max_new_claims < 1) or (
            deadline is not None and time.monotonic() + 16 >= deadline
        ):
            return {"receipt": "deferred"}
        result = _results(
            await self.database.prepare(
                "SELECT product_key,quantity FROM order_items WHERE order_id=? ORDER BY product_key"
            )
            .bind(order_id)
            .all()
        )
        if not result:
            raise ValueError("confirmed order has no items")
        purchase = Purchase(
            order_id, order["external_session_id"], tuple((row["product_key"], row["quantity"]) for row in result)
        )
        if deadline is not None and time.monotonic() + 16 >= deadline:
            return {"receipt": "deferred"}
        now = int(time.time())
        identity = hashlib.sha256(
            f"order-receipt-v1:{self.merchant}:{self.environment}:{order_id}".encode()
        ).hexdigest()
        claim = (
            await self.database.prepare(
                """INSERT OR IGNORE INTO post_purchase_executions
            (merchant_id,environment,order_id,workflow_key,action_digest,state,created_at,updated_at)
            SELECT ?,?,?,'order-receipt',?,'claimed',?,? WHERE NOT EXISTS (
              SELECT 1 FROM post_purchase_executions WHERE merchant_id=? AND environment=? AND order_id=?)"""
            )
            .bind(
                self.merchant, self.environment, order_id, identity, now, now, self.merchant, self.environment, order_id
            )
            .run()
        )
        if _changes(claim) != 1:
            return {"receipt": "pending_claim"}
        try:
            reference = self.handlers["receipt"](purchase)
            if isawaitable(reference):
                reference = await reference
            if not isinstance(reference, str) or not reference or len(reference) > 256:
                raise ValueError("handler did not confirm acceptance")
            updated = (
                await self.database.prepare(
                    """UPDATE post_purchase_executions SET state='accepted',provider_reference=?,updated_at=?
                WHERE merchant_id=? AND environment=? AND order_id=? AND workflow_key='order-receipt'
                AND state='claimed'"""
                )
                .bind(reference, int(time.time()), self.merchant, self.environment, order_id)
                .run()
            )
            if _changes(updated) != 1:
                raise RuntimeError("receipt acceptance was not recorded")
            return {"receipt": "accepted"}
        except asyncio.CancelledError:
            await self._hold_after_cancellation(order_id, "order-receipt")
            raise
        except Exception:
            try:
                await self._mark_uncertain(order_id, "order-receipt")
            except asyncio.CancelledError:
                await self._hold_after_cancellation(order_id, "order-receipt")
                raise
            return {"receipt": "uncertain"}
