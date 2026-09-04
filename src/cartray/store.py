from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from .errors import CheckoutInProgress, IdempotencyConflict, SettlementInconsistency
from .models import CheckoutOrder, CheckoutRedirect, OrderItem

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  checkout_request_id TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  manifest_version TEXT NOT NULL,
  items_digest TEXT NOT NULL,
  currency TEXT NOT NULL,
  subtotal_minor INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  product_key TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  stripe_price_id TEXT NOT NULL,
  unit_amount_minor INTEGER NOT NULL,
  fulfilment_resources_json TEXT NOT NULL,
  PRIMARY KEY (order_id, product_key)
);

CREATE TABLE IF NOT EXISTS checkout_sessions (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  state TEXT NOT NULL CHECK(state IN ('creating', 'session_created', 'sealed', 'redirect_issued', 'expired')),
  external_session_id TEXT UNIQUE,
  redirect_url TEXT,
  projection_nonce TEXT NOT NULL,
  lease_expires_at INTEGER,
  creation_attempt INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL,
  settlement_state TEXT NOT NULL DEFAULT 'pending' CHECK(settlement_state IN ('pending', 'confirmed', 'expired')),
  settlement_session_id TEXT,
  settlement_event_id TEXT,
  settled_at INTEGER,
  stripe_payment_status TEXT,
  stripe_amount_total_minor INTEGER,
  expiration_event_id TEXT,
  expired_at INTEGER
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,
  UNIQUE(order_id, event_type)
);

CREATE TABLE IF NOT EXISTS stripe_events (
  stripe_event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  session_id TEXT,
  payload_json TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  processed_at INTEGER,
  processing_error TEXT,
  payload_sha256 TEXT NOT NULL DEFAULT '',
  processing_state TEXT NOT NULL DEFAULT 'received'
    CHECK(processing_state IN ('received', 'ignored', 'confirmed', 'expired', 'failed'))
);

CREATE TABLE IF NOT EXISTS checkout_reconciliations (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  session_id TEXT NOT NULL UNIQUE,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  next_attempt_at INTEGER NOT NULL,
  lease_token TEXT,
  lease_expires_at INTEGER,
  last_attempt_at INTEGER,
  last_outcome TEXT CHECK(last_outcome IN (
    'open', 'unsettled', 'rejected', 'transient_error', 'confirmed', 'expired'
  )),
  last_error TEXT CHECK(last_error IN (
    'stripe_retrieval_failed', 'projection_rejected', 'reconciliation_rejected',
    'reconciliation_lookup_failed', 'checkout_not_settled', 'unrecognized_checkout_state'
  )),
  observed_status TEXT,
  observed_payment_status TEXT,
  observed_amount_total_minor INTEGER,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS outbox_undelivered ON outbox(delivered_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS checkout_sessions_settlement_session_id
ON checkout_sessions(settlement_session_id)
WHERE settlement_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS checkout_sessions_reconciliation_candidates
ON checkout_sessions(settlement_state, updated_at, external_session_id);
CREATE INDEX IF NOT EXISTS checkout_reconciliations_due
ON checkout_reconciliations(next_attempt_at, lease_expires_at, order_id);

CREATE TRIGGER IF NOT EXISTS orders_are_immutable
BEFORE UPDATE ON orders
BEGIN
  SELECT RAISE(ABORT, 'orders are immutable');
END;

CREATE TRIGGER IF NOT EXISTS order_items_are_immutable
BEFORE UPDATE ON order_items
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS order_items_cannot_be_deleted
BEFORE DELETE ON order_items
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS order_items_cannot_be_added_to_sealed_order
BEFORE INSERT ON order_items
WHEN EXISTS (SELECT 1 FROM checkout_sessions WHERE order_id = NEW.order_id)
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS checkout_redirect_issued_outbox
AFTER UPDATE OF state ON checkout_sessions
WHEN NEW.state = 'redirect_issued' AND OLD.redirect_url IS NULL
BEGIN
  INSERT OR IGNORE INTO outbox(order_id, event_type, payload_json, created_at)
  VALUES (NEW.order_id, 'CheckoutRedirectIssued',
          json_object('order_id', NEW.order_id, 'session_id', NEW.external_session_id), NEW.updated_at);
END;

CREATE TRIGGER IF NOT EXISTS checkout_session_settlement_is_monotonic
BEFORE UPDATE OF settlement_state ON checkout_sessions
WHEN NOT (OLD.settlement_state = 'pending' AND NEW.settlement_state IN ('confirmed', 'expired'))
BEGIN
  SELECT RAISE(ABORT, 'checkout settlement transition is invalid');
END;
"""


@dataclass(frozen=True)
class CheckoutStart:
    order_id: str
    owner: bool
    state: str
    nonce: str
    redirect: CheckoutRedirect | None


@dataclass(frozen=True)
class SettlementContext:
    order: CheckoutOrder
    nonce: str
    checkout_session_id: str
    settlement_state: str
    settlement_session_id: str | None


@dataclass(frozen=True)
class ReconciliationCandidate:
    order_id: str
    session_id: str
    lease_token: str
    attempt_count: int


class SettlementRejection(StrEnum):
    """Non-secret, operator-only stages for a retryable settlement rejection."""

    SESSION = "session_rejected"
    PROJECTION = "projection_rejected"
    ORDER_CONTEXT = "order_context_rejected"
    RECONCILIATION = "reconciliation_rejected"
    PREDICATE = "settlement_predicate_rejected"
    CONFIRMATION = "confirmation_rejected"


class SqliteOrderStore:
    """SQLite implementation shaped for D1's transactional subset."""

    def __init__(self, connection: sqlite3.Connection, *, lease_seconds: int = 60) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.lease_seconds = lease_seconds
        self.connection.executescript(SCHEMA)

    @classmethod
    def in_memory(cls, *, lease_seconds: int = 60) -> SqliteOrderStore:
        return cls(sqlite3.connect(":memory:"), lease_seconds=lease_seconds)

    async def start_or_load(self, order: CheckoutOrder, *, nonce: str, now: int | None = None) -> CheckoutStart:
        now = int(time.time()) if now is None else now
        with self.connection:
            row = self.connection.execute(
                """SELECT o.order_id, o.request_fingerprint, s.state, s.projection_nonce, s.external_session_id,
                          s.redirect_url, s.lease_expires_at
                   FROM orders o JOIN checkout_sessions s USING(order_id)
                   WHERE o.checkout_request_id = ?""",
                (order.checkout_request_id,),
            ).fetchone()
            if row is not None:
                if row["request_fingerprint"] != order.request_fingerprint:
                    raise IdempotencyConflict("checkout request ID was reused with a different cart")
                redirect = (
                    CheckoutRedirect(row["external_session_id"], row["redirect_url"]) if row["redirect_url"] else None
                )
                if redirect:
                    return CheckoutStart(row["order_id"], False, row["state"], row["projection_nonce"], redirect)
                if row["lease_expires_at"] is not None and row["lease_expires_at"] > now:
                    raise CheckoutInProgress("checkout creation is already in progress")
                self.connection.execute(
                    """UPDATE checkout_sessions
                       SET lease_expires_at = ?, creation_attempt = creation_attempt + 1, updated_at = ?
                       WHERE order_id = (SELECT order_id FROM orders WHERE checkout_request_id = ?)""",
                    (now + self.lease_seconds, now, order.checkout_request_id),
                )
                return CheckoutStart(row["order_id"], True, row["state"], row["projection_nonce"], None)

            self.connection.execute(
                """INSERT INTO orders
                   (order_id, checkout_request_id, request_fingerprint, manifest_version, items_digest,
                    currency, subtotal_minor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.order_id,
                    order.checkout_request_id,
                    order.request_fingerprint,
                    order.manifest_version,
                    order.items_digest,
                    order.currency,
                    order.subtotal_minor,
                    now,
                ),
            )
            self.connection.executemany(
                """INSERT INTO order_items
                   (order_id, product_key, quantity, stripe_price_id, unit_amount_minor,
                    fulfilment_resources_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        order.order_id,
                        item.product_key,
                        item.quantity,
                        item.stripe_price_id,
                        item.unit_amount_minor,
                        json.dumps(item.fulfilment_resources, separators=(",", ":")),
                    )
                    for item in order.items
                ],
            )
            self.connection.execute(
                """INSERT INTO checkout_sessions
                   (order_id, state, projection_nonce, lease_expires_at, updated_at)
                   VALUES (?, 'creating', ?, ?, ?)""",
                (order.order_id, nonce, now + self.lease_seconds, now),
            )
            self._insert_outbox(order.order_id, "OrderCreated", {"order_id": order.order_id}, now)
            return CheckoutStart(order.order_id, True, "creating", nonce, None)

    async def attach_redirect(self, order_id: str, redirect: CheckoutRedirect, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        with self.connection:
            updated = self.connection.execute(
                """UPDATE checkout_sessions
                   SET state = 'redirect_issued', external_session_id = ?, redirect_url = ?,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE order_id = ? AND redirect_url IS NULL""",
                (redirect.session_id, redirect.url, now, order_id),
            )
            if updated.rowcount == 1:
                return
            existing = self.connection.execute(
                "SELECT external_session_id, redirect_url FROM checkout_sessions WHERE order_id = ?", (order_id,)
            ).fetchone()
            if existing is not None and (existing["external_session_id"], existing["redirect_url"]) == (
                redirect.session_id,
                redirect.url,
            ):
                return
            raise CheckoutInProgress("checkout redirect was not persisted")

    def order_row(self, order_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        return row

    async def find_order_by_request(self, checkout_request_id: str, request_fingerprint: str) -> CheckoutOrder | None:
        row = self.connection.execute(
            """SELECT order_id, request_fingerprint
               FROM orders WHERE checkout_request_id = ?""",
            (checkout_request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != request_fingerprint:
            raise IdempotencyConflict("checkout request ID was reused with a different cart")
        return await self.load_order(row["order_id"])

    async def load_order(self, order_id: str) -> CheckoutOrder:
        order = self.order_row(order_id)
        items = tuple(
            OrderItem(
                product_key=row["product_key"],
                quantity=row["quantity"],
                stripe_price_id=row["stripe_price_id"],
                unit_amount_minor=row["unit_amount_minor"],
                fulfilment_resources=tuple(json.loads(row["fulfilment_resources_json"])),
            )
            for row in self.order_items(order_id)
        )
        return CheckoutOrder(
            order_id=order["order_id"],
            checkout_request_id=order["checkout_request_id"],
            request_fingerprint=order["request_fingerprint"],
            manifest_version=order["manifest_version"],
            items=items,
            items_digest=order["items_digest"],
            currency=order["currency"],
            subtotal_minor=order["subtotal_minor"],
        )

    def order_items(self, order_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY product_key", (order_id,)
        ).fetchall()

    def outbox_events(self, order_id: str) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM outbox WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()

    async def checkout_status(self, session_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT settlement_state FROM checkout_sessions WHERE external_session_id = ?", (session_id,)
        ).fetchone()
        return None if row is None else row["settlement_state"]

    async def claim_reconciliation_candidates(
        self, *, now: int, stale_before: int, limit: int, lease_seconds: int
    ) -> tuple[ReconciliationCandidate, ...]:
        if limit < 1 or lease_seconds < 1:
            raise ValueError("reconciliation claim bounds must be positive")
        with self.connection:
            rows = self.connection.execute(
                """SELECT s.order_id, s.external_session_id
                   FROM checkout_sessions AS s
                   LEFT JOIN checkout_reconciliations AS r USING(order_id)
                   WHERE s.settlement_state = 'pending'
                     AND s.external_session_id IS NOT NULL
                     AND s.updated_at <= ?
                     AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?)
                     AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= ?)
                   ORDER BY COALESCE(r.next_attempt_at, s.updated_at), s.order_id
                   LIMIT ?""",
                (stale_before, now, now, limit),
            ).fetchall()
            claimed: list[ReconciliationCandidate] = []
            for row in rows:
                order_id, session_id = row["order_id"], row["external_session_id"]
                token = uuid4().hex
                self.connection.execute(
                    """INSERT OR IGNORE INTO checkout_reconciliations
                       (order_id, session_id, next_attempt_at, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (order_id, session_id, now, now),
                )
                updated = self.connection.execute(
                    """UPDATE checkout_reconciliations
                       SET attempt_count = attempt_count + 1, lease_token = ?, lease_expires_at = ?,
                           last_attempt_at = ?, updated_at = ?
                       WHERE order_id = ? AND session_id = ? AND next_attempt_at <= ?
                         AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                         AND EXISTS (
                           SELECT 1 FROM checkout_sessions
                           WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                         )""",
                    (token, now + lease_seconds, now, now, order_id, session_id, now, now, order_id, session_id),
                )
                if updated.rowcount == 1:
                    attempt_count = self.connection.execute(
                        "SELECT attempt_count FROM checkout_reconciliations WHERE order_id = ?", (order_id,)
                    ).fetchone()["attempt_count"]
                    claimed.append(ReconciliationCandidate(order_id, session_id, token, attempt_count))
            return tuple(claimed)

    async def finish_reconciliation_retry(
        self,
        *,
        candidate: ReconciliationCandidate,
        now: int,
        next_attempt_at: int,
        outcome: str,
        error: str | None,
        observed_status: str | None = None,
        observed_payment_status: str | None = None,
        observed_amount_total_minor: int | None = None,
    ) -> bool:
        _validate_reconciliation_outcome(outcome, terminal=False)
        with self.connection:
            updated = self.connection.execute(
                """UPDATE checkout_reconciliations
                   SET next_attempt_at = ?, lease_token = NULL, lease_expires_at = NULL, last_outcome = ?,
                       last_error = ?, observed_status = ?, observed_payment_status = ?,
                       observed_amount_total_minor = ?, updated_at = ?
                   WHERE order_id = ? AND session_id = ? AND lease_token = ?
                     AND EXISTS (
                       SELECT 1 FROM checkout_sessions
                       WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                     )""",
                (
                    next_attempt_at,
                    outcome,
                    error,
                    observed_status,
                    observed_payment_status,
                    observed_amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    candidate.order_id,
                    candidate.session_id,
                ),
            )
            return updated.rowcount == 1

    async def confirm_reconciliation(
        self,
        *,
        candidate: ReconciliationCandidate,
        payment_status: str,
        amount_total_minor: int,
        now: int,
        items_digest: str,
    ) -> bool:
        with self.connection:
            checkout = self.connection.execute(
                "SELECT settlement_state FROM checkout_sessions WHERE order_id = ? AND external_session_id = ?",
                (candidate.order_id, candidate.session_id),
            ).fetchone()
            if checkout is None:
                raise SettlementInconsistency("CartRay reconciliation has no Checkout Session")
            if checkout["settlement_state"] == "expired":
                raise SettlementInconsistency("an expired CartRay Checkout Session cannot be confirmed")
            if checkout["settlement_state"] == "confirmed":
                return True
            updated = self.connection.execute(
                """UPDATE checkout_sessions
                   SET settlement_state = 'confirmed', settlement_session_id = ?, settled_at = ?,
                       stripe_payment_status = ?, stripe_amount_total_minor = ?, updated_at = ?
                   WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                     AND EXISTS (
                       SELECT 1 FROM checkout_reconciliations
                       WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                     )""",
                (
                    candidate.session_id,
                    now,
                    payment_status,
                    amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise SettlementInconsistency("CartRay reconciliation confirmation transition is invalid")
            audit = self.connection.execute(
                """UPDATE checkout_reconciliations
                   SET lease_token = NULL, lease_expires_at = NULL, last_outcome = 'confirmed', last_error = NULL,
                       observed_status = 'complete', observed_payment_status = ?,
                       observed_amount_total_minor = ?, updated_at = ?
                   WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?""",
                (
                    payment_status,
                    amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
            )
            if audit.rowcount != 1:
                raise SettlementInconsistency("CartRay reconciliation claim is invalid")
            self._insert_outbox(
                candidate.order_id,
                "OrderConfirmed",
                {
                    "order_id": candidate.order_id,
                    "session_id": candidate.session_id,
                    "items_digest": items_digest,
                },
                now,
            )
            return False

    async def expire_reconciliation(
        self, *, candidate: ReconciliationCandidate, now: int, payment_status: str, amount_total_minor: int
    ) -> bool:
        with self.connection:
            checkout = self.connection.execute(
                "SELECT settlement_state FROM checkout_sessions WHERE order_id = ? AND external_session_id = ?",
                (candidate.order_id, candidate.session_id),
            ).fetchone()
            if checkout is None:
                raise SettlementInconsistency("CartRay reconciliation has no Checkout Session")
            if checkout["settlement_state"] == "confirmed":
                raise SettlementInconsistency("a confirmed CartRay Checkout Session cannot expire")
            if checkout["settlement_state"] == "expired":
                return True
            updated = self.connection.execute(
                """UPDATE checkout_sessions
                   SET settlement_state = 'expired', expired_at = ?, updated_at = ?
                   WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                     AND EXISTS (
                       SELECT 1 FROM checkout_reconciliations
                       WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                     )""",
                (
                    now,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise SettlementInconsistency("CartRay reconciliation expiry transition is invalid")
            audit = self.connection.execute(
                """UPDATE checkout_reconciliations
                   SET lease_token = NULL, lease_expires_at = NULL, last_outcome = 'expired', last_error = NULL,
                       observed_status = 'expired', observed_payment_status = ?,
                       observed_amount_total_minor = ?, updated_at = ?
                   WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?""",
                (
                    payment_status,
                    amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
            )
            if audit.rowcount != 1:
                raise SettlementInconsistency("CartRay reconciliation claim is invalid")
            return False

    async def expire_checkout(
        self,
        *,
        event_id: str,
        session_id: str,
        payload: dict[str, object],
        payload_sha256: str,
        now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        with self.connection:
            existing = self.connection.execute(
                "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if existing["event_type"] != "checkout.session.expired":
                    raise SettlementInconsistency("Stripe event ID is bound to a different event type")
                if existing["session_id"] != session_id:
                    raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
                if existing["processing_state"] == "expired":
                    return True
                raise SettlementInconsistency("Stripe event has an inconsistent expiry record")
            checkout = self.connection.execute(
                "SELECT order_id, settlement_state FROM checkout_sessions WHERE external_session_id = ?", (session_id,)
            ).fetchone()
            if checkout is None:
                raise SettlementInconsistency("Stripe expiry event has no CartRay Checkout Session")
            if checkout["settlement_state"] == "confirmed":
                raise SettlementInconsistency("a confirmed CartRay Checkout Session cannot expire")
            if checkout["settlement_state"] not in {"pending", "expired"}:
                raise SettlementInconsistency("CartRay Checkout Session has an invalid expiry state")
            duplicate = checkout["settlement_state"] == "expired"
            if not duplicate:
                updated = self.connection.execute(
                    """UPDATE checkout_sessions
                       SET settlement_state = 'expired', expiration_event_id = ?, expired_at = ?, updated_at = ?
                       WHERE order_id = ? AND settlement_state = 'pending'""",
                    (event_id, now, now, checkout["order_id"]),
                )
                if updated.rowcount != 1:
                    raise SettlementInconsistency("CartRay Checkout Session expiry transition is invalid")
            self.connection.execute(
                """INSERT INTO stripe_events
                   (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                    payload_sha256, processing_state)
                   VALUES (?, 'checkout.session.expired', ?, ?, ?, ?, ?, 'expired')""",
                (event_id, session_id, json.dumps(payload, separators=(",", ":")), now, now, payload_sha256),
            )
            return duplicate

    async def settlement_context(self, order_id: str) -> SettlementContext:
        row = self.connection.execute(
            """SELECT projection_nonce, external_session_id, settlement_state, settlement_session_id
               FROM checkout_sessions WHERE order_id = ?""",
            (order_id,),
        ).fetchone()
        if row is None or not isinstance(row["external_session_id"], str):
            raise SettlementInconsistency("CartRay order has no Checkout Session")
        return SettlementContext(
            await self.load_order(order_id),
            row["projection_nonce"],
            row["external_session_id"],
            row["settlement_state"],
            row["settlement_session_id"],
        )

    async def begin_settlement_event(
        self,
        *,
        event_id: str,
        session_id: str,
        payload: dict[str, object],
        payload_sha256: str,
        now: int | None = None,
    ) -> bool:
        """Return true only when this logical Stripe event is already confirmed."""
        now = int(time.time()) if now is None else now
        with self.connection:
            existing = self.connection.execute(
                "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if existing["event_type"] != "checkout.session.completed":
                    raise SettlementInconsistency("Stripe event ID is bound to a different event type")
                if existing["session_id"] != session_id:
                    raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
                if existing["processing_state"] == "confirmed":
                    return True
                if existing["processing_state"] != "received":
                    raise SettlementInconsistency("Stripe event has an inconsistent settlement record")
                return False
            self.connection.execute(
                """INSERT INTO stripe_events
                   (stripe_event_id, event_type, session_id, payload_json, received_at,
                    payload_sha256, processing_state)
                   VALUES (?, 'checkout.session.completed', ?, ?, ?, ?, 'received')""",
                (event_id, session_id, json.dumps(payload, separators=(",", ":")), now, payload_sha256),
            )
        return False

    async def record_settlement_rejection(
        self,
        *,
        event_id: str,
        session_id: str,
        rejection: SettlementRejection,
    ) -> None:
        if not isinstance(rejection, SettlementRejection):
            raise ValueError("settlement rejection must be a known operator diagnostic")
        with self.connection:
            self.connection.execute(
                """UPDATE stripe_events SET processing_error = ?
                   WHERE stripe_event_id = ? AND event_type = 'checkout.session.completed'
                     AND session_id = ? AND processing_state = 'received'""",
                (rejection.value, event_id, session_id),
            )

    async def record_ignored_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        payload_sha256: str,
        now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        with self.connection:
            existing = self.connection.execute(
                "SELECT event_type, processing_state FROM stripe_events WHERE stripe_event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing["event_type"] != event_type:
                    raise SettlementInconsistency("Stripe event ID is bound to a different event type")
                return existing["processing_state"] == "ignored"
            self.connection.execute(
                """INSERT INTO stripe_events
                   (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                    payload_sha256, processing_state)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, 'ignored')""",
                (event_id, event_type, json.dumps(payload, separators=(",", ":")), now, now, payload_sha256),
            )
        return False

    async def confirm_settlement(
        self,
        *,
        order_id: str,
        session_id: str,
        event_id: str,
        payload: dict[str, object],
        payload_sha256: str,
        payment_status: str,
        amount_total_minor: int,
        now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        with self.connection:
            existing = self.connection.execute(
                "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?",
                (event_id,),
            ).fetchone()
            resume = False
            if existing is not None:
                if existing["event_type"] != "checkout.session.completed":
                    raise SettlementInconsistency("Stripe event ID is bound to a different event type")
                if existing["session_id"] != session_id:
                    raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
                if existing["processing_state"] == "confirmed":
                    return True
                if existing["processing_state"] != "received":
                    raise SettlementInconsistency("Stripe event has an inconsistent unfinished settlement record")
                resume = True
            context = await self.settlement_context(order_id)
            if context.checkout_session_id != session_id or (
                context.settlement_session_id is not None and context.settlement_session_id != session_id
            ):
                raise SettlementInconsistency("CartRay order is bound to a different Checkout Session")
            if context.settlement_state == "expired":
                raise SettlementInconsistency("an expired CartRay Checkout Session cannot be confirmed")
            if resume:
                self.connection.execute(
                    """UPDATE stripe_events
                       SET processed_at = ?, processing_error = NULL, processing_state = 'confirmed'
                       WHERE stripe_event_id = ? AND event_type = 'checkout.session.completed'
                         AND session_id = ? AND processing_state = 'received'""",
                    (now, event_id, session_id),
                )
            else:
                self.connection.execute(
                    """INSERT INTO stripe_events
                       (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                        payload_sha256, processing_state)
                       VALUES (?, 'checkout.session.completed', ?, ?, ?, ?, ?, 'confirmed')""",
                    (event_id, session_id, json.dumps(payload, separators=(",", ":")), now, now, payload_sha256),
                )
            if context.settlement_state == "confirmed":
                return True
            self.connection.execute(
                """UPDATE checkout_sessions
                   SET settlement_state = 'confirmed', settlement_session_id = ?, settlement_event_id = ?,
                       settled_at = ?, stripe_payment_status = ?, stripe_amount_total_minor = ?, updated_at = ?
                   WHERE order_id = ? AND settlement_state = 'pending'""",
                (session_id, event_id, now, payment_status, amount_total_minor, now, order_id),
            )
            self._insert_outbox(
                order_id,
                "OrderConfirmed",
                {"order_id": order_id, "session_id": session_id, "items_digest": context.order.items_digest},
                now,
            )
            return False

    def _insert_outbox(self, order_id: str, event_type: str, payload: dict[str, str], now: int) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO outbox(order_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (order_id, event_type, json.dumps(payload, separators=(",", ":")), now),
        )


class D1OrderStore:
    """Async D1 implementation of the checkout order state machine."""

    def __init__(self, database, *, lease_seconds: int = 60) -> None:
        self.database = database
        self.lease_seconds = lease_seconds

    async def start_or_load(self, order: CheckoutOrder, *, nonce: str, now: int | None = None) -> CheckoutStart:
        now = int(time.time()) if now is None else now
        existing = await self._checkout_start(order.checkout_request_id, order.request_fingerprint)
        if existing is not None:
            if existing.redirect is not None:
                return existing
            if existing.state == "creating" and await self._lease_is_current(existing.order_id, now):
                raise CheckoutInProgress("checkout creation is already in progress")
            result = (
                await self.database.prepare(
                    "UPDATE checkout_sessions SET lease_expires_at = ?, creation_attempt = creation_attempt + 1, "
                    "updated_at = ? WHERE order_id = ? AND redirect_url IS NULL "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
                )
                .bind(now + self.lease_seconds, now, existing.order_id, now)
                .run()
            )
            if _changes(result) != 1:
                raise CheckoutInProgress("checkout creation is already in progress")
            return CheckoutStart(existing.order_id, True, existing.state, existing.nonce, None)

        statements = [
            self.database.prepare(
                "INSERT INTO orders (order_id, checkout_request_id, request_fingerprint, manifest_version, "
                "items_digest, currency, subtotal_minor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                order.order_id,
                order.checkout_request_id,
                order.request_fingerprint,
                order.manifest_version,
                order.items_digest,
                order.currency,
                order.subtotal_minor,
                now,
            )
        ]
        statements.extend(
            self.database.prepare(
                """INSERT INTO order_items (order_id, product_key, quantity, stripe_price_id, unit_amount_minor,
                   fulfilment_resources_json) VALUES (?, ?, ?, ?, ?, ?)"""
            ).bind(
                order.order_id,
                item.product_key,
                item.quantity,
                item.stripe_price_id,
                item.unit_amount_minor,
                json.dumps(item.fulfilment_resources, separators=(",", ":")),
            )
            for item in order.items
        )
        statements.extend(
            [
                self.database.prepare(
                    """INSERT INTO checkout_sessions (order_id, state, projection_nonce, lease_expires_at, updated_at)
                       VALUES (?, 'creating', ?, ?, ?)"""
                ).bind(order.order_id, nonce, now + self.lease_seconds, now),
                self.database.prepare(
                    "INSERT INTO outbox(order_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)"
                ).bind(order.order_id, "OrderCreated", json.dumps({"order_id": order.order_id}), now),
            ]
        )
        try:
            await self.database.batch(statements)
        except Exception:
            winner = await self._checkout_start(order.checkout_request_id, order.request_fingerprint)
            if winner is None:
                raise
            return await self.start_or_load(order, nonce=nonce, now=now)
        return CheckoutStart(order.order_id, True, "creating", nonce, None)

    async def attach_redirect(self, order_id: str, redirect: CheckoutRedirect, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        updated = await (
            self.database.prepare(
                """UPDATE checkout_sessions SET state = 'redirect_issued', external_session_id = ?, redirect_url = ?,
               lease_expires_at = NULL, updated_at = ? WHERE order_id = ? AND redirect_url IS NULL"""
            )
            .bind(redirect.session_id, redirect.url, now, order_id)
            .run()
        )
        if _changes(updated) == 1:
            return
        existing = await self._checkout_start_by_order(order_id)
        if existing is not None and existing.redirect == redirect:
            return
        raise CheckoutInProgress("checkout redirect was not persisted")

    async def find_order_by_request(self, checkout_request_id: str, request_fingerprint: str) -> CheckoutOrder | None:
        row = await self._first(
            "SELECT order_id, request_fingerprint FROM orders WHERE checkout_request_id = ?", checkout_request_id
        )
        if row is None:
            return None
        if row["request_fingerprint"] != request_fingerprint:
            raise IdempotencyConflict("checkout request ID was reused with a different cart")
        return await self.load_order(row["order_id"])

    async def checkout_status(self, session_id: str) -> str | None:
        row = await self._first(
            "SELECT settlement_state FROM checkout_sessions WHERE external_session_id = ?", session_id
        )
        return None if row is None else row["settlement_state"]

    async def claim_reconciliation_candidates(
        self, *, now: int, stale_before: int, limit: int, lease_seconds: int
    ) -> tuple[ReconciliationCandidate, ...]:
        if limit < 1 or lease_seconds < 1:
            raise ValueError("reconciliation claim bounds must be positive")
        result = (
            await self.database.prepare(
                """SELECT s.order_id, s.external_session_id
                   FROM checkout_sessions AS s
                   LEFT JOIN checkout_reconciliations AS r USING(order_id)
                   WHERE s.settlement_state = 'pending'
                     AND s.external_session_id IS NOT NULL
                     AND s.updated_at <= ?
                     AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?)
                     AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= ?)
                   ORDER BY COALESCE(r.next_attempt_at, s.updated_at), s.order_id
                   LIMIT ?"""
            )
            .bind(stale_before, now, now, limit)
            .all()
        )
        claimed: list[ReconciliationCandidate] = []
        for row in _results(result):
            order_id, session_id = row["order_id"], row["external_session_id"]
            token = uuid4().hex
            await (
                self.database.prepare(
                    """INSERT OR IGNORE INTO checkout_reconciliations
                       (order_id, session_id, next_attempt_at, updated_at)
                       VALUES (?, ?, ?, ?)"""
                )
                .bind(order_id, session_id, now, now)
                .run()
            )
            claimed_result = await (
                self.database.prepare(
                    """UPDATE checkout_reconciliations
                       SET attempt_count = attempt_count + 1, lease_token = ?, lease_expires_at = ?,
                           last_attempt_at = ?, updated_at = ?
                       WHERE order_id = ? AND session_id = ? AND next_attempt_at <= ?
                         AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                         AND EXISTS (
                           SELECT 1 FROM checkout_sessions
                           WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                         )"""
                )
                .bind(token, now + lease_seconds, now, now, order_id, session_id, now, now, order_id, session_id)
                .run()
            )
            if _changes(claimed_result) != 1:
                continue
            claimed_row = await self._first(
                "SELECT attempt_count FROM checkout_reconciliations WHERE order_id = ?", order_id
            )
            if claimed_row is None:
                raise SettlementInconsistency("CartRay reconciliation claim disappeared")
            claimed.append(ReconciliationCandidate(order_id, session_id, token, claimed_row["attempt_count"]))
        return tuple(claimed)

    async def finish_reconciliation_retry(
        self,
        *,
        candidate: ReconciliationCandidate,
        now: int,
        next_attempt_at: int,
        outcome: str,
        error: str | None,
        observed_status: str | None = None,
        observed_payment_status: str | None = None,
        observed_amount_total_minor: int | None = None,
    ) -> bool:
        _validate_reconciliation_outcome(outcome, terminal=False)
        result = await (
            self.database.prepare(
                """UPDATE checkout_reconciliations
                   SET next_attempt_at = ?, lease_token = NULL, lease_expires_at = NULL, last_outcome = ?,
                       last_error = ?, observed_status = ?, observed_payment_status = ?,
                       observed_amount_total_minor = ?, updated_at = ?
                   WHERE order_id = ? AND session_id = ? AND lease_token = ?
                     AND EXISTS (
                       SELECT 1 FROM checkout_sessions
                       WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                     )"""
            )
            .bind(
                next_attempt_at,
                outcome,
                error,
                observed_status,
                observed_payment_status,
                observed_amount_total_minor,
                now,
                candidate.order_id,
                candidate.session_id,
                candidate.lease_token,
                candidate.order_id,
                candidate.session_id,
            )
            .run()
        )
        return _changes(result) == 1

    async def confirm_reconciliation(
        self,
        *,
        candidate: ReconciliationCandidate,
        payment_status: str,
        amount_total_minor: int,
        now: int,
        items_digest: str,
    ) -> bool:
        checkout = await self._first(
            "SELECT settlement_state FROM checkout_sessions WHERE order_id = ? AND external_session_id = ?",
            candidate.order_id,
            candidate.session_id,
        )
        if checkout is None:
            raise SettlementInconsistency("CartRay reconciliation has no Checkout Session")
        if checkout["settlement_state"] == "expired":
            raise SettlementInconsistency("an expired CartRay Checkout Session cannot be confirmed")
        if checkout["settlement_state"] == "confirmed":
            return True
        results = await self.database.batch(
            [
                self.database.prepare(
                    """UPDATE checkout_sessions
                       SET settlement_state = 'confirmed', settlement_session_id = ?, settled_at = ?,
                           stripe_payment_status = ?, stripe_amount_total_minor = ?, updated_at = ?
                       WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                         AND EXISTS (
                           SELECT 1 FROM checkout_reconciliations
                           WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                         )"""
                ).bind(
                    candidate.session_id,
                    now,
                    payment_status,
                    amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
                self.database.prepare(
                    """INSERT OR IGNORE INTO outbox(order_id, event_type, payload_json, created_at)
                       SELECT ?, 'OrderConfirmed', ?, ?
                       WHERE EXISTS (
                         SELECT 1 FROM checkout_sessions
                         WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'confirmed'
                           AND settlement_event_id IS NULL
                       )
                         AND EXISTS (
                           SELECT 1 FROM checkout_reconciliations
                           WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                         )"""
                ).bind(
                    candidate.order_id,
                    json.dumps(
                        {
                            "order_id": candidate.order_id,
                            "session_id": candidate.session_id,
                            "items_digest": items_digest,
                        },
                        separators=(",", ":"),
                    ),
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
                self.database.prepare(
                    """UPDATE checkout_reconciliations
                       SET lease_token = NULL, lease_expires_at = NULL, last_outcome = 'confirmed', last_error = NULL,
                           observed_status = 'complete', observed_payment_status = ?,
                           observed_amount_total_minor = ?, updated_at = ?
                       WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                         AND EXISTS (
                           SELECT 1 FROM checkout_sessions
                           WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'confirmed'
                         )"""
                ).bind(
                    payment_status,
                    amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                ),
            ]
        )
        if _changes(results[0]) == 1 and _changes(results[2]) == 1:
            return False
        state = await self.checkout_status(candidate.session_id)
        if state == "confirmed":
            return True
        if state == "expired":
            raise SettlementInconsistency("an expired CartRay Checkout Session cannot be confirmed")
        raise SettlementInconsistency("CartRay reconciliation confirmation transition is invalid")

    async def expire_reconciliation(
        self, *, candidate: ReconciliationCandidate, now: int, payment_status: str, amount_total_minor: int
    ) -> bool:
        checkout = await self._first(
            "SELECT settlement_state FROM checkout_sessions WHERE order_id = ? AND external_session_id = ?",
            candidate.order_id,
            candidate.session_id,
        )
        if checkout is None:
            raise SettlementInconsistency("CartRay reconciliation has no Checkout Session")
        if checkout["settlement_state"] == "confirmed":
            raise SettlementInconsistency("a confirmed CartRay Checkout Session cannot expire")
        if checkout["settlement_state"] == "expired":
            return True
        results = await self.database.batch(
            [
                self.database.prepare(
                    """UPDATE checkout_sessions
                       SET settlement_state = 'expired', expired_at = ?, updated_at = ?
                       WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'pending'
                         AND EXISTS (
                           SELECT 1 FROM checkout_reconciliations
                           WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                         )"""
                ).bind(
                    now,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                ),
                self.database.prepare(
                    """UPDATE checkout_reconciliations
                       SET lease_token = NULL, lease_expires_at = NULL, last_outcome = 'expired', last_error = NULL,
                           observed_status = 'expired', observed_payment_status = ?,
                           observed_amount_total_minor = ?, updated_at = ?
                       WHERE order_id = ? AND session_id = ? AND lease_token = ? AND lease_expires_at > ?
                         AND EXISTS (
                           SELECT 1 FROM checkout_sessions
                           WHERE order_id = ? AND external_session_id = ? AND settlement_state = 'expired'
                         )"""
                ).bind(
                    payment_status,
                    amount_total_minor,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                    candidate.lease_token,
                    now,
                    candidate.order_id,
                    candidate.session_id,
                ),
            ]
        )
        if _changes(results[0]) == 1 and _changes(results[1]) == 1:
            return False
        state = await self.checkout_status(candidate.session_id)
        if state == "expired":
            return True
        if state == "confirmed":
            raise SettlementInconsistency("a confirmed CartRay Checkout Session cannot expire")
        raise SettlementInconsistency("CartRay reconciliation expiry transition is invalid")

    async def expire_checkout(
        self,
        *,
        event_id: str,
        session_id: str,
        payload: dict[str, object],
        payload_sha256: str,
        now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        existing = await self._first(
            "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?", event_id
        )
        if existing is not None:
            if existing["event_type"] != "checkout.session.expired":
                raise SettlementInconsistency("Stripe event ID is bound to a different event type")
            if existing["session_id"] != session_id:
                raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
            if existing["processing_state"] == "expired":
                return True
            raise SettlementInconsistency("Stripe event has an inconsistent expiry record")
        checkout = await self._first(
            "SELECT order_id, settlement_state FROM checkout_sessions WHERE external_session_id = ?", session_id
        )
        if checkout is None:
            raise SettlementInconsistency("Stripe expiry event has no CartRay Checkout Session")
        if checkout["settlement_state"] == "confirmed":
            raise SettlementInconsistency("a confirmed CartRay Checkout Session cannot expire")
        if checkout["settlement_state"] not in {"pending", "expired"}:
            raise SettlementInconsistency("CartRay Checkout Session has an invalid expiry state")
        event_statement = self.database.prepare(
            """INSERT INTO stripe_events
               (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                payload_sha256, processing_state)
               VALUES (?, 'checkout.session.expired', ?, ?, ?, ?, ?, 'expired')"""
        ).bind(event_id, session_id, json.dumps(payload, separators=(",", ":")), now, now, payload_sha256)
        if checkout["settlement_state"] == "expired":
            try:
                await event_statement.run()
            except Exception:
                duplicate = await self._duplicate_expiry_event(event_id, session_id)
                if duplicate is not None:
                    return duplicate
                raise
            return True
        try:
            results = await self.database.batch(
                [
                    self.database.prepare(
                        """UPDATE checkout_sessions
                           SET settlement_state = 'expired', expiration_event_id = ?, expired_at = ?, updated_at = ?
                           WHERE order_id = ? AND settlement_state = 'pending'"""
                    ).bind(event_id, now, now, checkout["order_id"]),
                    self.database.prepare(
                        """INSERT INTO stripe_events
                           (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                            payload_sha256, processing_state)
                           SELECT ?, 'checkout.session.expired', ?, ?, ?, ?, ?, 'expired'
                           WHERE EXISTS (
                             SELECT 1 FROM checkout_sessions
                             WHERE external_session_id = ? AND settlement_state = 'expired'
                           )"""
                    ).bind(
                        event_id,
                        session_id,
                        json.dumps(payload, separators=(",", ":")),
                        now,
                        now,
                        payload_sha256,
                        session_id,
                    ),
                ]
            )
        except Exception:
            duplicate = await self._duplicate_expiry_event(event_id, session_id)
            if duplicate is not None:
                return duplicate
            raise
        if _changes(results[0]) == 1:
            return False
        if await self.checkout_status(session_id) == "expired":
            return True
        raise SettlementInconsistency("CartRay Checkout Session expiry transition is invalid")

    async def settlement_context(self, order_id: str) -> SettlementContext:
        row = await self._first(
            """SELECT projection_nonce, external_session_id, settlement_state, settlement_session_id
               FROM checkout_sessions WHERE order_id = ?""",
            order_id,
        )
        if row is None or not isinstance(row["external_session_id"], str):
            raise SettlementInconsistency("CartRay order has no Checkout Session")
        return SettlementContext(
            await self.load_order(order_id),
            row["projection_nonce"],
            row["external_session_id"],
            row["settlement_state"],
            row["settlement_session_id"],
        )

    async def begin_settlement_event(
        self,
        *,
        event_id: str,
        session_id: str,
        payload: dict[str, object],
        payload_sha256: str,
        now: int | None = None,
    ) -> bool:
        """Return true only when this logical Stripe event is already confirmed."""
        now = int(time.time()) if now is None else now
        existing = await self._first(
            "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?", event_id
        )
        if existing is not None:
            if existing["event_type"] != "checkout.session.completed":
                raise SettlementInconsistency("Stripe event ID is bound to a different event type")
            if existing["session_id"] != session_id:
                raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
            if existing["processing_state"] == "confirmed":
                return True
            if existing["processing_state"] != "received":
                raise SettlementInconsistency("Stripe event has an inconsistent settlement record")
            return False
        statement = self.database.prepare(
            """INSERT INTO stripe_events
               (stripe_event_id, event_type, session_id, payload_json, received_at,
                payload_sha256, processing_state)
               VALUES (?, 'checkout.session.completed', ?, ?, ?, ?, 'received')"""
        ).bind(event_id, session_id, json.dumps(payload, separators=(",", ":")), now, payload_sha256)
        try:
            await statement.run()
        except Exception:
            duplicate = await self._first(
                "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?",
                event_id,
            )
            if duplicate is None:
                raise
            if duplicate["event_type"] != "checkout.session.completed" or duplicate["session_id"] != session_id:
                raise SettlementInconsistency("Stripe event ID has an inconsistent duplicate record")
            if duplicate["processing_state"] == "confirmed":
                return True
            if duplicate["processing_state"] == "received":
                return False
            raise SettlementInconsistency("Stripe event has an inconsistent settlement record")
        return False

    async def record_settlement_rejection(
        self,
        *,
        event_id: str,
        session_id: str,
        rejection: SettlementRejection,
    ) -> None:
        if not isinstance(rejection, SettlementRejection):
            raise ValueError("settlement rejection must be a known operator diagnostic")
        await (
            self.database.prepare(
                """UPDATE stripe_events SET processing_error = ?
                   WHERE stripe_event_id = ? AND event_type = 'checkout.session.completed'
                     AND session_id = ? AND processing_state = 'received'"""
            )
            .bind(rejection.value, event_id, session_id)
            .run()
        )

    async def record_ignored_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        payload_sha256: str,
        now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        existing = await self._first(
            "SELECT event_type, processing_state FROM stripe_events WHERE stripe_event_id = ?", event_id
        )
        if existing is not None:
            if existing["event_type"] != event_type:
                raise SettlementInconsistency("Stripe event ID is bound to a different event type")
            return existing["processing_state"] == "ignored"
        statement = self.database.prepare(
            """INSERT INTO stripe_events
               (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                payload_sha256, processing_state)
               VALUES (?, ?, NULL, ?, ?, ?, ?, 'ignored')"""
        ).bind(event_id, event_type, json.dumps(payload, separators=(",", ":")), now, now, payload_sha256)
        try:
            await statement.run()
        except Exception:
            duplicate = await self._first(
                "SELECT event_type, processing_state FROM stripe_events WHERE stripe_event_id = ?", event_id
            )
            if duplicate is not None:
                if duplicate["event_type"] != event_type:
                    raise SettlementInconsistency("Stripe event ID is bound to a different event type")
                if duplicate["processing_state"] == "ignored":
                    return True
            raise
        return False

    async def confirm_settlement(
        self,
        *,
        order_id: str,
        session_id: str,
        event_id: str,
        payload: dict[str, object],
        payload_sha256: str,
        payment_status: str,
        amount_total_minor: int,
        now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        existing = await self._first(
            "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?", event_id
        )
        resume = False
        if existing is not None:
            if existing["event_type"] != "checkout.session.completed":
                raise SettlementInconsistency("Stripe event ID is bound to a different event type")
            if existing["session_id"] != session_id:
                raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
            if existing["processing_state"] == "confirmed":
                return True
            if existing["processing_state"] != "received":
                raise SettlementInconsistency("Stripe event has an inconsistent unfinished settlement record")
            resume = True
        context = await self.settlement_context(order_id)
        if context.checkout_session_id != session_id or (
            context.settlement_session_id is not None and context.settlement_session_id != session_id
        ):
            raise SettlementInconsistency("CartRay order is bound to a different Checkout Session")
        if context.settlement_state == "expired":
            raise SettlementInconsistency("an expired CartRay Checkout Session cannot be confirmed")
        if resume:
            statements = [
                self.database.prepare(
                    """UPDATE stripe_events
                       SET processed_at = ?, processing_error = NULL, processing_state = 'confirmed'
                       WHERE stripe_event_id = ? AND event_type = 'checkout.session.completed'
                         AND session_id = ? AND processing_state = 'received'"""
                ).bind(now, event_id, session_id)
            ]
        else:
            statements = [
                self.database.prepare(
                    """INSERT INTO stripe_events
                       (stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
                        payload_sha256, processing_state)
                       VALUES (?, 'checkout.session.completed', ?, ?, ?, ?, ?, 'confirmed')"""
                ).bind(event_id, session_id, json.dumps(payload, separators=(",", ":")), now, now, payload_sha256)
            ]
        if context.settlement_state == "pending":
            statements.extend(
                [
                    self.database.prepare(
                        """UPDATE checkout_sessions
                           SET settlement_state = 'confirmed', settlement_session_id = ?, settlement_event_id = ?,
                               settled_at = ?, stripe_payment_status = ?, stripe_amount_total_minor = ?, updated_at = ?
                           WHERE order_id = ? AND settlement_state = 'pending'"""
                    ).bind(session_id, event_id, now, payment_status, amount_total_minor, now, order_id),
                    self.database.prepare(
                        "INSERT OR IGNORE INTO outbox(order_id, event_type, payload_json, created_at) "
                        "VALUES (?, ?, ?, ?)"
                    ).bind(
                        order_id,
                        "OrderConfirmed",
                        json.dumps(
                            {
                                "order_id": order_id,
                                "session_id": session_id,
                                "items_digest": context.order.items_digest,
                            },
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                ]
            )
        try:
            await self.database.batch(statements)
        except Exception:
            duplicate = await self._first(
                "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?", event_id
            )
            if duplicate is not None:
                if duplicate["event_type"] != "checkout.session.completed":
                    raise SettlementInconsistency("Stripe event ID is bound to a different event type")
                if duplicate["session_id"] != session_id:
                    raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
                if duplicate["processing_state"] == "confirmed":
                    return True
            raise
        return context.settlement_state == "confirmed"

    async def load_order(self, order_id: str) -> CheckoutOrder:
        order = await self._first("SELECT * FROM orders WHERE order_id = ?", order_id)
        if order is None:
            raise KeyError(order_id)
        item_result = (
            await self.database.prepare("SELECT * FROM order_items WHERE order_id = ? ORDER BY product_key")
            .bind(order_id)
            .all()
        )
        items = tuple(
            OrderItem(
                product_key=row["product_key"],
                quantity=row["quantity"],
                stripe_price_id=row["stripe_price_id"],
                unit_amount_minor=row["unit_amount_minor"],
                fulfilment_resources=tuple(json.loads(row["fulfilment_resources_json"])),
            )
            for row in _results(item_result)
        )
        return CheckoutOrder(
            order_id=order["order_id"],
            checkout_request_id=order["checkout_request_id"],
            request_fingerprint=order["request_fingerprint"],
            manifest_version=order["manifest_version"],
            items=items,
            items_digest=order["items_digest"],
            currency=order["currency"],
            subtotal_minor=order["subtotal_minor"],
        )

    async def _checkout_start(self, request_id: str, fingerprint: str) -> CheckoutStart | None:
        row = await self._first(
            "SELECT o.order_id, o.request_fingerprint, s.state, s.projection_nonce, s.external_session_id, "
            "s.redirect_url FROM orders o JOIN checkout_sessions s USING(order_id) WHERE o.checkout_request_id = ?",
            request_id,
        )
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint:
            raise IdempotencyConflict("checkout request ID was reused with a different cart")
        redirect = CheckoutRedirect(row["external_session_id"], row["redirect_url"]) if row["redirect_url"] else None
        return CheckoutStart(row["order_id"], False, row["state"], row["projection_nonce"], redirect)

    async def _checkout_start_by_order(self, order_id: str) -> CheckoutStart | None:
        row = await self._first(
            "SELECT order_id, state, projection_nonce, external_session_id, redirect_url "
            "FROM checkout_sessions WHERE order_id = ?",
            order_id,
        )
        if row is None:
            return None
        redirect = CheckoutRedirect(row["external_session_id"], row["redirect_url"]) if row["redirect_url"] else None
        return CheckoutStart(row["order_id"], False, row["state"], row["projection_nonce"], redirect)

    async def _lease_is_current(self, order_id: str, now: int) -> bool:
        row = await self._first("SELECT lease_expires_at FROM checkout_sessions WHERE order_id = ?", order_id)
        return row is not None and row["lease_expires_at"] is not None and row["lease_expires_at"] > now

    async def _duplicate_expiry_event(self, event_id: str, session_id: str) -> bool | None:
        duplicate = await self._first(
            "SELECT event_type, processing_state, session_id FROM stripe_events WHERE stripe_event_id = ?", event_id
        )
        if duplicate is None:
            return None
        if duplicate["event_type"] != "checkout.session.expired":
            raise SettlementInconsistency("Stripe event ID is bound to a different event type")
        if duplicate["session_id"] != session_id:
            raise SettlementInconsistency("Stripe event ID is bound to a different Checkout Session")
        if duplicate["processing_state"] == "expired":
            return True
        raise SettlementInconsistency("Stripe event has an inconsistent expiry record")

    async def _first(self, sql: str, *params):
        result = await self.database.prepare(sql).bind(*params).all()
        rows = _results(result)
        return rows[0] if rows else None


def _results(result) -> list[dict]:
    rows = getattr(result, "results", result.get("results", []) if isinstance(result, dict) else [])
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in rows]


def _changes(result) -> int:
    meta = getattr(result, "meta", result.get("meta", {}) if isinstance(result, dict) else {})
    if hasattr(meta, "to_py"):
        meta = meta.to_py()
    return int(meta.get("changes", 0) if isinstance(meta, dict) else getattr(meta, "changes", 0))


def _validate_reconciliation_outcome(outcome: str, *, terminal: bool) -> None:
    allowed = {"confirmed", "expired"} if terminal else {"open", "unsettled", "rejected", "transient_error"}
    if outcome not in allowed:
        raise ValueError("invalid reconciliation outcome")
