"""Bounded post-purchase actions over confirmed commerce outbox records.

Handlers are trusted server-side implementations. A claimed external side effect
is never automatically retried: an unknown outcome requires operator review.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class Action:
    workflow_key: str
    offer_keys: tuple[str, ...]
    handler: str
    config_ref: str
    config_digest: str

    def __post_init__(self):
        if (
            not _IDENTIFIER.fullmatch(self.workflow_key)
            or not self.offer_keys
            or len(self.offer_keys) > 100
            or len(set(self.offer_keys)) != len(self.offer_keys)
            or any(not _IDENTIFIER.fullmatch(offer) for offer in self.offer_keys)
            or not _IDENTIFIER.fullmatch(self.handler)
            or not _IDENTIFIER.fullmatch(self.config_ref)
            or not re.fullmatch(r"[0-9a-f]{64}", self.config_digest)
        ):
            raise ValueError("invalid post-purchase action")


@dataclass(frozen=True)
class Execution:
    merchant: str
    environment: str
    workflow_key: str
    config_ref: str


@dataclass(frozen=True)
class Purchase:
    order_id: str
    session_id: str
    items: tuple[tuple[str, int], ...]


class SqliteDispatcher:
    """Consume a specific durable event without changing another consumer's cursor."""

    def __init__(
        self, connection: sqlite3.Connection, merchant: str, environment: str, handlers: Mapping[str, Callable]
    ):
        if not _IDENTIFIER.fullmatch(merchant) or not _IDENTIFIER.fullmatch(environment):
            raise ValueError("invalid merchant identity")
        binding = connection.execute(
            "SELECT merchant_id, environment FROM post_purchase_installation WHERE singleton=1"
        ).fetchone()
        if not binding or tuple(binding) != (merchant, environment):
            raise ValueError("post-purchase installation binding mismatch")
        self.connection, self.merchant, self.environment, self.handlers = (
            connection,
            merchant,
            environment,
            dict(handlers),
        )

    def dispatch(self, order_id: str, actions: tuple[Action, ...]) -> dict[str, str]:
        if self.connection.in_transaction:
            raise ValueError("post-purchase dispatch requires a dedicated idle connection")
        if not _IDENTIFIER.fullmatch(order_id) or len(actions) > 100:
            raise ValueError("invalid dispatch request")
        if len({action.workflow_key for action in actions}) != len(actions):
            raise ValueError("duplicate workflow identity")
        for action in actions:
            if action.handler not in self.handlers:
                raise ValueError("unregistered post-purchase handler")
        order = self.connection.execute(
            """SELECT s.external_session_id FROM checkout_sessions s
               WHERE s.order_id=? AND s.settlement_state='confirmed' AND EXISTS
                 (SELECT 1 FROM outbox e WHERE e.order_id=s.order_id AND e.event_type='OrderConfirmed')""",
            (order_id,),
        ).fetchone()
        if not order or not order[0]:
            raise ValueError("purchase is not confirmed")
        items = tuple(
            self.connection.execute(
                "SELECT product_key, quantity FROM order_items WHERE order_id=? ORDER BY product_key", (order_id,)
            ).fetchall()
        )
        purchase = Purchase(order_id, order[0], tuple((row[0], row[1]) for row in items))
        purchased = {product for product, _ in purchase.items}
        # Validate all existing action identities before any external effect.
        digests = {}
        for action in actions:
            digest = hashlib.sha256(
                json.dumps(
                    [action.handler, action.config_ref, action.config_digest, sorted(action.offer_keys)],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            existing = self.connection.execute(
                """SELECT action_digest FROM post_purchase_executions
                WHERE merchant_id=? AND environment=? AND order_id=? AND workflow_key=?""",
                (self.merchant, self.environment, order_id, action.workflow_key),
            ).fetchone()
            if existing and existing[0] != digest:
                raise ValueError("existing workflow identity has different configuration")
            digests[action.workflow_key] = digest
        outcomes = {}
        for action in actions:
            if not purchased.intersection(action.offer_keys):
                continue
            now = int(time.time())
            digest = digests[action.workflow_key]
            with self.connection:
                inserted = self.connection.execute(
                    """INSERT OR IGNORE INTO post_purchase_executions
                    (merchant_id,environment,order_id,workflow_key,action_digest,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,'claimed',?,?)""",
                    (self.merchant, self.environment, order_id, action.workflow_key, digest, now, now),
                ).rowcount
            if not inserted:
                existing = self.connection.execute(
                    """SELECT action_digest FROM post_purchase_executions
                    WHERE merchant_id=? AND environment=? AND order_id=? AND workflow_key=?""",
                    (self.merchant, self.environment, order_id, action.workflow_key),
                ).fetchone()
                if not existing or existing[0] != digest:
                    raise ValueError("existing workflow identity has different configuration")
                outcomes[action.workflow_key] = "already_claimed"
                continue
            try:
                reference = self.handlers[action.handler](
                    purchase, Execution(self.merchant, self.environment, action.workflow_key, action.config_ref)
                )
                if not isinstance(reference, str) or not reference or len(reference) > 256:
                    raise ValueError("handler did not confirm acceptance")
                with self.connection:
                    self.connection.execute(
                        """UPDATE post_purchase_executions SET state='accepted',provider_reference=?,updated_at=?
                        WHERE merchant_id=? AND environment=? AND order_id=? AND workflow_key=? AND state='claimed'""",
                        (reference, int(time.time()), self.merchant, self.environment, order_id, action.workflow_key),
                    )
                outcomes[action.workflow_key] = "accepted"
            except Exception:
                with self.connection:
                    self.connection.execute(
                        """UPDATE post_purchase_executions SET state='uncertain',updated_at=?
                        WHERE merchant_id=? AND environment=? AND order_id=? AND workflow_key=? AND state='claimed'""",
                        (int(time.time()), self.merchant, self.environment, order_id, action.workflow_key),
                    )
                outcomes[action.workflow_key] = "uncertain"
        return outcomes
