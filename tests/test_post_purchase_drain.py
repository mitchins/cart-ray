from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from cartray.post_purchase import Action, D1Dispatcher, drain_confirmed_d1

ROOT = Path(__file__).resolve().parents[1]


class Statement:
    def __init__(self, db, sql):
        self.db, self.sql, self.args = db, sql, ()

    def bind(self, *args):
        self.args = args
        return self

    async def all(self):
        return {"results": [dict(row) for row in self.db.execute(self.sql, self.args)]}

    async def run(self):
        result = self.db.execute(self.sql, self.args)
        self.db.commit()
        return {"meta": {"changes": result.rowcount}}


class Database:
    def __init__(self, db):
        self.db = db

    def prepare(self, sql):
        return Statement(self.db, sql)


def setup_database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE orders(order_id TEXT PRIMARY KEY, items_digest TEXT);
        CREATE TABLE checkout_sessions(order_id TEXT, external_session_id TEXT, settlement_state TEXT);
        CREATE TABLE order_items(order_id TEXT, product_key TEXT, quantity INTEGER);
        CREATE TABLE outbox(id INTEGER PRIMARY KEY, order_id TEXT, event_type TEXT,
                            payload_json TEXT, delivered_at INTEGER);
        INSERT INTO orders VALUES ('before','old-digest'),('after','new-digest');
        INSERT INTO checkout_sessions VALUES ('before','old-session','confirmed'),('after','new-session','confirmed');
        INSERT INTO order_items VALUES ('before','product',1),('after','product',1);
    """)
    for ident, order in [(1, "before"), (2, "after")]:
        session, digest = ("old-session", "old-digest") if order == "before" else ("new-session", "new-digest")
        db.execute(
            "INSERT INTO outbox VALUES (?,?,'OrderConfirmed',?,NULL)",
            (ident, order, json.dumps({"order_id": order, "session_id": session, "items_digest": digest})),
        )
    db.executescript((ROOT / "migrations/0006_post_purchase_actions.sql").read_text())
    db.executescript((ROOT / "migrations/0007_post_purchase_checkpoint.sql").read_text())
    db.execute("INSERT INTO post_purchase_installation VALUES (1,'store','test')")
    db.execute("INSERT INTO post_purchase_checkpoint VALUES (1,'store','test',1,1)")
    db.commit()
    return db


def test_drain_preserves_watermark_and_replay():
    db = setup_database()
    calls = []
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *args: calls.append(args) or "ok"})
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    assert asyncio.run(drain_confirmed_d1(dispatcher, (action,))) == 1
    assert asyncio.run(drain_confirmed_d1(dispatcher, (action,))) == 0
    assert [call[0].order_id for call in calls] == ["after"]
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL").fetchone()[0] == 0


def test_unresolved_claim_stops_checkpoint_without_resending():
    db = setup_database()
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *_: "receipt"})
    assert asyncio.run(dispatcher.dispatch("after", (action,))) == {"workflow": "accepted"}
    db.execute("UPDATE post_purchase_executions SET state='claimed', provider_reference=NULL")
    db.commit()
    dispatcher.handlers["service"] = lambda *_: pytest.fail("must not resend an unresolved claim")
    assert asyncio.run(drain_confirmed_d1(dispatcher, (action,))) == 0
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 1


def test_invalid_event_never_calls_handler_or_advances_checkpoint():
    db = setup_database()
    db.execute("UPDATE outbox SET payload_json=? WHERE id=2", (json.dumps({"order_id": "wrong"}),))
    db.commit()
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *_: pytest.fail("must not send")})
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(drain_confirmed_d1(dispatcher, (action,)))
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM post_purchase_executions").fetchone()[0] == 0


@pytest.mark.parametrize("checkpoint", ["missing", "wrong_store", "wrong_environment"])
def test_missing_or_mismatched_checkpoint_never_dispatches(checkpoint):
    db = setup_database()
    if checkpoint == "missing":
        db.execute("DELETE FROM post_purchase_checkpoint")
    elif checkpoint == "wrong_store":
        db.execute("UPDATE post_purchase_checkpoint SET merchant_id='other'")
    else:
        db.execute("UPDATE post_purchase_checkpoint SET environment='other'")
    db.commit()
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *_: pytest.fail("must not send")})
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    with pytest.raises(ValueError, match="checkpoint"):
        asyncio.run(drain_confirmed_d1(dispatcher, (action,)))
    assert db.execute("SELECT COUNT(*) FROM post_purchase_executions").fetchone()[0] == 0


@pytest.mark.parametrize("limit", [0, 51])
def test_drain_rejects_unbounded_batches(limit):
    dispatcher = D1Dispatcher(Database(setup_database()), "store", "test", {})
    with pytest.raises(ValueError, match="batch limit"):
        asyncio.run(drain_confirmed_d1(dispatcher, (), limit=limit))


def test_missing_installation_binding_leaves_event_unclaimed():
    db = setup_database()
    db.execute("DROP TRIGGER post_purchase_installation_no_delete")
    db.execute("DELETE FROM post_purchase_installation")
    db.commit()
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *_: pytest.fail("must not send")})
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    with pytest.raises(ValueError, match="binding mismatch"):
        asyncio.run(drain_confirmed_d1(dispatcher, (action,)))
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 1


def test_d1_configuration_change_is_rejected_before_external_effect():
    db = setup_database()
    calls = []
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *args: calls.append(args) or "ok"})
    first = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    assert asyncio.run(dispatcher.dispatch("after", (first,))) == {"workflow": "accepted"}
    changed = Action("workflow", ("product",), "service", "private/config", "b" * 64)
    with pytest.raises(ValueError, match="different configuration"):
        asyncio.run(dispatcher.dispatch("after", (changed,)))
    assert len(calls) == 1


@pytest.mark.parametrize("response_status,expected_state", [(200, "accepted"), (503, "uncertain")])
def test_worker_drain_calls_scoped_handler_and_never_replays(monkeypatch, response_status, expected_state):
    import sys
    from types import ModuleType, SimpleNamespace

    import cartray.worker as worker

    db = setup_database()
    seen = []
    action = {
        "workflow_key": "delivery",
        "offer_keys": ["product"],
        "handler": "service",
        "config_ref": "private/config",
        "config_digest": "a" * 64,
    }

    class Request:
        @staticmethod
        def new(url, options):
            return SimpleNamespace(url=url, options=options)

    class Service:
        async def fetch(self, request):
            seen.append(request)

            async def response_json():
                return {"provider_reference": "receipt"}

            return SimpleNamespace(status=response_status, json=response_json)

    js = ModuleType("js")
    js.Request = Request
    js.Object = SimpleNamespace(fromEntries=lambda value: value)
    pyodide = ModuleType("pyodide")
    ffi = ModuleType("pyodide.ffi")
    ffi.to_js = lambda value, **kwargs: value
    monkeypatch.setitem(sys.modules, "js", js)
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)
    env = SimpleNamespace(
        DB=Database(db),
        CARTRAY_ENVIRONMENT="test",
        CARTRAY_POST_PURCHASE_MODE="test",
        CARTRAY_POST_PURCHASE_TOKEN="t" * 32,
        POST_PURCHASE_HANDLER=Service(),
        CARTRAY_POST_PURCHASE_CONFIG=json.dumps({"merchant": "store", "environment": "test", "actions": [action]}),
    )
    asyncio.run(worker._drain_post_purchase(env))
    asyncio.run(worker._drain_post_purchase(env))
    assert len(seen) == 1
    assert seen[0].url == "https://post-purchase-handler.internal/"
    assert seen[0].options["headers"]["authorization"] == "Bearer " + "t" * 32
    body = json.loads(seen[0].options["body"])
    assert (body["merchant"], body["environment"], body["order_id"], body["workflow_key"]) == (
        "store",
        "test",
        "after",
        "delivery",
    )
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == expected_state
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 2
