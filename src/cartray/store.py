from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .errors import CheckoutInProgress, IdempotencyConflict
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
  updated_at INTEGER NOT NULL
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
  processing_error TEXT
);

CREATE INDEX IF NOT EXISTS checkout_sessions_external_session_id ON checkout_sessions(external_session_id);
CREATE INDEX IF NOT EXISTS outbox_undelivered ON outbox(delivered_at, id);

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
"""


@dataclass(frozen=True)
class CheckoutStart:
    order_id: str
    owner: bool
    state: str
    nonce: str
    redirect: CheckoutRedirect | None


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
