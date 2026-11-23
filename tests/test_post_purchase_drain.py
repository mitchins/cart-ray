from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from cartray.post_purchase import Action, D1Dispatcher, OrderReceiptDispatcher, drain_confirmed_d1

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
    db.executescript((ROOT / "migrations/0008_post_purchase_recovery.sql").read_text())
    db.executescript((ROOT / "migrations/0010_receipt_handover.sql").read_text())
    db.execute("INSERT INTO post_purchase_installation VALUES (1,'store','test')")
    db.execute("INSERT INTO post_purchase_checkpoint VALUES (1,'store','test',1,1)")
    db.commit()
    return db


@pytest.mark.parametrize("first,second", [("order-receipt", "old-workflow"), ("old-workflow", "order-receipt")])
def test_handover_does_not_allow_legacy_and_receipt_claims_for_same_order(first, second):
    db = setup_database()
    statement = """INSERT OR IGNORE INTO post_purchase_executions
        (merchant_id,environment,order_id,workflow_key,action_digest,state,created_at,updated_at)
        VALUES ('store','test','after',?,?,'claimed',1,1)"""
    assert db.execute(statement, (first, 'a' * 64)).rowcount == 1
    assert db.execute(statement, (second, 'b' * 64)).rowcount == 0
    assert db.execute("SELECT workflow_key FROM post_purchase_executions").fetchone()[0] == first


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


def test_operator_hold_of_stale_claim_unblocks_later_purchase_without_resend():
    db = setup_database()
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    calls = []
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda p, _: calls.append(p.order_id) or "ok"})
    assert asyncio.run(dispatcher.dispatch("after", (action,))) == {"workflow": "accepted"}
    db.execute("UPDATE post_purchase_executions SET state='claimed', provider_reference=NULL")
    audit = """INSERT INTO post_purchase_recovery_audit
        SELECT merchant_id,environment,order_id,workflow_key,action_digest,'operator','receipt unknown',
               CAST(strftime('%s','now') AS INTEGER)
        FROM post_purchase_executions WHERE order_id='after'"""
    with pytest.raises(sqlite3.IntegrityError, match="not old enough"):
        db.execute(audit)
    db.execute("UPDATE post_purchase_executions SET updated_at=CAST(strftime('%s','now') AS INTEGER)-1201")
    with pytest.raises(sqlite3.IntegrityError, match="mismatched"):
        db.execute("""INSERT INTO post_purchase_recovery_audit
          SELECT merchant_id,environment,order_id,workflow_key,'wrong','operator','receipt unknown',
            CAST(strftime('%s','now') AS INTEGER) FROM post_purchase_executions WHERE order_id='after'""")
    db.execute(audit)
    db.execute("INSERT INTO orders VALUES ('later','later-digest')")
    db.execute("INSERT INTO checkout_sessions VALUES ('later','later-session','confirmed')")
    db.execute("INSERT INTO order_items VALUES ('later','product',1)")
    db.execute("INSERT INTO outbox VALUES (3,'later','OrderConfirmed',?,NULL)",
               (json.dumps({"order_id": "later", "session_id": "later-session", "items_digest": "later-digest"}),))
    db.commit()
    assert asyncio.run(drain_confirmed_d1(dispatcher, (action,))) == 2
    assert calls == ["after", "later"]
    assert db.execute("SELECT state FROM post_purchase_executions WHERE order_id='after'").fetchone()[0] == "uncertain"
    assert db.execute("SELECT operator,evidence FROM post_purchase_recovery_audit").fetchone()[:] == (
        "operator", "receipt unknown"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM post_purchase_recovery_audit")


def test_drain_bounds_external_calls_and_resumes_overlapping_workflows():
    db = setup_database()
    calls = []
    dispatcher = D1Dispatcher(
        Database(db), "store", "test", {"service": lambda _, e: calls.append(e.workflow_key) or "ok"}
    )
    actions = tuple(Action(f"flow-{n}", ("product",), "service", f"private/{n}", "a" * 64) for n in range(3))
    assert asyncio.run(drain_confirmed_d1(dispatcher, actions, max_new_claims=2)) == 0
    assert calls == ["flow-0", "flow-1"]
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 1
    assert asyncio.run(drain_confirmed_d1(dispatcher, actions, max_new_claims=2)) == 1
    assert calls == ["flow-0", "flow-1", "flow-2"]


def test_drain_does_not_claim_when_time_budget_is_insufficient():
    db = setup_database()
    calls = []
    dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": lambda *_: calls.append(1) or "ok"})
    action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
    assert asyncio.run(drain_confirmed_d1(dispatcher, (action,), deadline=time.monotonic() + 12)) == 0
    assert not calls
    assert db.execute("SELECT COUNT(*) FROM post_purchase_executions").fetchone()[0] == 0
    assert asyncio.run(drain_confirmed_d1(dispatcher, (action,), deadline=time.monotonic() + 30)) == 1
    assert len(calls) == 1


def test_many_actions_do_not_require_one_validation_query_each():
    class D1BoundedStatement(Statement):
        def bind(self, *args):
            assert len(args) <= 100
            return super().bind(*args)

    class CountingDatabase(Database):
        count = 0

        def prepare(self, sql):
            self.count += 1
            return D1BoundedStatement(self.db, sql)

    database = CountingDatabase(setup_database())
    dispatcher = D1Dispatcher(database, "store", "test", {"service": lambda *_: "ok"})
    actions = tuple(Action(f"flow-{n}", ("product",), "service", f"private/{n}", "a" * 64) for n in range(100))
    outcomes = asyncio.run(dispatcher.dispatch("after", actions, max_new_claims=2))
    assert list(outcomes.values()).count("accepted") == 2
    assert list(outcomes.values()).count("deferred") == 98
    assert database.count <= 9


def test_cancelled_handler_is_held_before_checkpoint_replay():
    db = setup_database()
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def handler(*_):
            calls.append(1)
            entered.set()
            await asyncio.Event().wait()

        dispatcher = D1Dispatcher(Database(db), "store", "test", {"service": handler})
        action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
        task = asyncio.create_task(drain_confirmed_d1(dispatcher, (action,)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "uncertain"
        assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 1
        assert await drain_confirmed_d1(dispatcher, (action,)) == 1

    asyncio.run(scenario())
    assert len(calls) == 1


def test_cancellation_during_uncertain_write_attempts_the_hold_again():
    db = setup_database()

    async def scenario():
        started = asyncio.Event()
        writes = 0

        class DelayedStatement(Statement):
            async def run(self):
                nonlocal writes
                if "SET state='uncertain'" in self.sql:
                    writes += 1
                    if writes == 1:
                        started.set()
                        await asyncio.Event().wait()
                return await super().run()

        class DelayedDatabase(Database):
            def prepare(self, sql):
                return DelayedStatement(self.db, sql)

        def unavailable(*_):
            raise RuntimeError("synthetic provider timeout")

        dispatcher = D1Dispatcher(DelayedDatabase(db), "store", "test", {"service": unavailable})
        action = Action("workflow", ("product",), "service", "private/config", "a" * 64)
        task = asyncio.create_task(drain_confirmed_d1(dispatcher, (action,)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writes == 2
        assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "uncertain"
        assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 1

    asyncio.run(scenario())


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
        CARTRAY_MERCHANT_ID="store",
    )
    asyncio.run(worker._drain_post_purchase(env))
    asyncio.run(worker._drain_post_purchase(env))
    assert len(seen) == 1
    assert seen[0].url == "https://post-purchase-handler.internal/"
    assert seen[0].options["headers"]["authorization"] == "Bearer " + "t" * 32
    body = json.loads(seen[0].options["body"])
    assert (body["merchant"], body["environment"], body["order_id"], body["type"]) == (
        "store", "test", "after", "order_confirmation"
    )
    assert body["items"] == [{"product_key": "product", "quantity": 1, "title": "product"}]
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == expected_state
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 2


def test_worker_handler_timeout_is_held_and_never_resent(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    import cartray.worker as worker

    db = setup_database()
    calls = []

    class Request:
        @staticmethod
        def new(url, options):
            return SimpleNamespace(url=url, options=options)

    class Service:
        async def fetch(self, request):
            calls.append(request)
            await asyncio.sleep(1)

    js = ModuleType("js")
    js.Request = Request
    js.Object = SimpleNamespace(fromEntries=lambda value: value)
    pyodide = ModuleType("pyodide")
    ffi = ModuleType("pyodide.ffi")
    ffi.to_js = lambda value, **kwargs: value
    monkeypatch.setitem(sys.modules, "js", js)
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)
    original_wait_for = asyncio.wait_for

    async def short_handler_timeout(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.01 if timeout == 8 else timeout)

    monkeypatch.setattr(worker.asyncio, "wait_for", short_handler_timeout)
    env = SimpleNamespace(
        DB=Database(db), CARTRAY_ENVIRONMENT="test", CARTRAY_POST_PURCHASE_MODE="test",
        CARTRAY_POST_PURCHASE_TOKEN="t" * 32, POST_PURCHASE_HANDLER=Service(),
        CARTRAY_MERCHANT_ID="store",
    )
    asyncio.run(worker._drain_post_purchase(env))
    asyncio.run(worker._drain_post_purchase(env))
    assert len(calls) == 1
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "uncertain"
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 2


def test_legacy_workflow_binding_does_not_authorize_order_wide_receipts():
    from types import SimpleNamespace

    from cartray.worker import _drain_post_purchase

    env = SimpleNamespace(CARTRAY_ENVIRONMENT="test", CARTRAY_POST_PURCHASE_MODE="test",
                          CARTRAY_POST_PURCHASE_CONFIG='{"legacy":true}')
    asyncio.run(_drain_post_purchase(env))


def test_order_receipt_collapses_workflows_and_replay_without_backfill():
    db = setup_database()
    db.execute("INSERT INTO order_items VALUES ('after','second',2)")
    db.commit()
    sent = []
    dispatcher = OrderReceiptDispatcher(Database(db), "store", "test",
                                        {"receipt": lambda purchase: sent.append(purchase) or "accepted"})
    actions = (Action("first", ("product",), "service", "a", "a" * 64),
               Action("second", ("second",), "service", "b", "b" * 64))
    assert asyncio.run(drain_confirmed_d1(dispatcher, actions)) == 1
    assert asyncio.run(drain_confirmed_d1(dispatcher, actions)) == 0
    assert len(sent) == 1 and sent[0].items == (("product", 1), ("second", 2))
    assert db.execute("SELECT workflow_key,state FROM post_purchase_executions").fetchall()[0][:] == (
        "order-receipt", "accepted"
    )
    assert db.execute("SELECT last_outbox_id FROM post_purchase_checkpoint").fetchone()[0] == 2


def test_order_receipt_holds_unknown_and_never_resends_legacy_claim():
    db = setup_database()
    calls = []
    def failed(purchase):
        calls.append(purchase.order_id)
        raise TimeoutError()
    dispatcher = OrderReceiptDispatcher(Database(db), "store", "test", {"receipt": failed})
    assert asyncio.run(dispatcher.dispatch("after")) == {"receipt": "uncertain"}
    assert asyncio.run(dispatcher.dispatch("after")) == {"receipt": "already_claimed"}
    assert calls == ["after"]
    db.execute("DELETE FROM post_purchase_executions")
    db.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,?,?,'claimed',NULL,?,?)",
               ("store", "test", "after", "legacy", "a" * 64, 1, 1))
    db.commit()
    assert asyncio.run(dispatcher.dispatch("after")) == {"receipt": "pending_claim"}
    assert calls == ["after"]
