import sqlite3
from pathlib import Path

import pytest

from cartray.post_purchase import Action, SqliteDispatcher

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/0006_post_purchase_actions.sql"


def setup_store(confirmed=True, event=True):
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE orders(order_id TEXT PRIMARY KEY);
        CREATE TABLE order_items(order_id TEXT, product_key TEXT, quantity INTEGER);
        CREATE TABLE checkout_sessions(order_id TEXT, external_session_id TEXT, settlement_state TEXT);
        CREATE TABLE outbox(order_id TEXT, event_type TEXT, delivered_at INTEGER);
        INSERT INTO orders VALUES ('order1');
        INSERT INTO order_items VALUES ('order1','one',2),('order1','two',1);
    """)
    db.execute(
        "INSERT INTO checkout_sessions VALUES (?,?,?)", ("order1", "cs_test_a", "confirmed" if confirmed else "pending")
    )
    if event:
        db.execute("INSERT INTO outbox VALUES ('order1','OrderConfirmed',NULL)")
    db.executescript(MIGRATION.read_text())
    db.execute("INSERT INTO post_purchase_installation VALUES (1,'store','test')")
    db.commit()
    return db


def test_distinct_workflows_execute_once_for_same_order_and_payload():
    db = setup_store()
    calls = []

    def handler(purchase, config):
        calls.append((purchase, config))
        return "provider-reference"

    dispatcher = SqliteDispatcher(db, "store", "test", {"email": handler})
    actions = (
        Action("first", ("one", "two"), "email", "same", "a" * 64),
        Action("second", ("two",), "email", "same", "a" * 64),
    )
    assert dispatcher.dispatch("order1", actions) == {"first": "accepted", "second": "accepted"}
    assert dispatcher.dispatch("order1", actions) == {"first": "already_claimed", "second": "already_claimed"}
    assert len(calls) == 2
    assert calls[0][0].items == (("one", 2), ("two", 1))
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None


@pytest.mark.parametrize("confirmed,event", [(False, True), (True, False)])
def test_requires_both_authoritative_confirmed_order_and_outbox(confirmed, event):
    db = setup_store(confirmed, event)
    dispatcher = SqliteDispatcher(db, "store", "test", {"email": lambda *_: pytest.fail("must not send")})
    with pytest.raises(ValueError, match="not confirmed"):
        dispatcher.dispatch("order1", (Action("first", ("one",), "email", "config", "a" * 64),))


def test_uncertainty_never_automatically_retries_and_configuration_is_pinned():
    db = setup_store()
    calls = []

    def handler(*args):
        calls.append(args)
        raise TimeoutError()

    dispatcher = SqliteDispatcher(db, "store", "test", {"email": handler})
    action = Action("first", ("one",), "email", "config", "a" * 64)
    assert dispatcher.dispatch("order1", (action,)) == {"first": "uncertain"}
    assert dispatcher.dispatch("order1", (action,)) == {"first": "already_claimed"}
    assert len(calls) == 1
    with pytest.raises(ValueError, match="different configuration"):
        dispatcher.dispatch("order1", (Action("first", ("one",), "email", "different", "a" * 64),))


def test_scope_and_unmatched_offers():
    db = setup_store()
    calls = []

    def handler(*args):
        calls.append(args)
        return "ok"

    action = Action("first", ("one",), "email", "config", "a" * 64)
    missing = Action("missing", ("other",), "email", "config", "a" * 64)
    dispatcher = SqliteDispatcher(db, "store", "test", {"email": handler})
    assert dispatcher.dispatch("order1", (action, missing)) == {"first": "accepted"}
    for merchant, environment in [("other", "test"), ("store", "other")]:
        with pytest.raises(ValueError, match="binding mismatch"):
            SqliteDispatcher(db, merchant, environment, {"email": handler})
    assert len(calls) == 1


def test_changed_offer_mapping_and_resolved_config_rejected_even_if_no_longer_matches():
    db = setup_store()
    dispatcher = SqliteDispatcher(db, "store", "test", {"email": lambda *_: "ok"})
    assert dispatcher.dispatch("order1", (Action("first", ("one",), "email", "config", "a" * 64),)) == {
        "first": "accepted"
    }
    with pytest.raises(ValueError, match="different configuration"):
        dispatcher.dispatch("order1", (Action("first", ("other",), "email", "config", "a" * 64),))
    with pytest.raises(ValueError, match="different configuration"):
        dispatcher.dispatch("order1", (Action("first", ("one",), "email", "config", "b" * 64),))


def test_handler_receives_workflow_and_merchant_scope():
    db = setup_store()
    seen = []
    dispatcher = SqliteDispatcher(
        db, "store", "test", {"callback": lambda purchase, execution: seen.append(execution) or "ok"}
    )
    dispatcher.dispatch("order1", (Action("first", ("one",), "callback", "config", "a" * 64),))
    assert (seen[0].merchant, seen[0].environment, seen[0].workflow_key, seen[0].config_ref) == (
        "store",
        "test",
        "first",
        "config",
    )


def test_batch_conflict_is_rejected_before_any_new_handler_runs():
    db = setup_store()
    calls = []
    dispatcher = SqliteDispatcher(db, "store", "test", {"email": lambda *args: calls.append(args) or "ok"})
    dispatcher.dispatch("order1", (Action("existing", ("one",), "email", "config", "a" * 64),))
    with pytest.raises(ValueError, match="different configuration"):
        dispatcher.dispatch(
            "order1",
            (
                Action("new", ("one",), "email", "config", "a" * 64),
                Action("existing", ("one",), "email", "changed", "a" * 64),
            ),
        )
    assert len(calls) == 1


def test_rejects_active_caller_transaction_without_committing():
    db = setup_store()
    dispatcher = SqliteDispatcher(db, "store", "test", {"email": lambda *_: "ok"})
    db.execute("INSERT INTO orders VALUES ('uncommitted')")
    with pytest.raises(ValueError, match="dedicated idle connection"):
        dispatcher.dispatch("order1", (Action("first", ("one",), "email", "config", "a" * 64),))
    db.rollback()
    assert db.execute("SELECT 1 FROM orders WHERE order_id='uncommitted'").fetchone() is None


def test_two_concurrent_consumers_claim_one_action(tmp_path):
    import concurrent.futures
    import threading

    path = tmp_path / "orders.sqlite"
    base = setup_store()
    with sqlite3.connect(path) as target:
        base.backup(target)
    base.close()
    barrier = threading.Barrier(2)
    calls = []

    def worker():
        with sqlite3.connect(path, timeout=5) as db:
            dispatcher = SqliteDispatcher(db, "store", "test", {"email": lambda *args: calls.append(args) or "ok"})
            barrier.wait(timeout=5)
            return dispatcher.dispatch("order1", (Action("first", ("one",), "email", "config", "a" * 64),))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(result["first"] for result in results) == ["accepted", "already_claimed"]
    assert len(calls) == 1


def test_config_references_allow_namespaces_but_reject_traversal():
    action = Action("delivery", ("one",), "email", "private/merchant/dummy-delivery", "a" * 64)
    assert action.config_ref == "private/merchant/dummy-delivery"
    with pytest.raises(ValueError, match="invalid post-purchase action"):
        Action("delivery", ("one",), "email", "private/../credentials", "a" * 64)


def test_async_d1_adapter_executes_distinct_workflows_and_replays():
    import asyncio

    from cartray.post_purchase import D1Dispatcher

    db = setup_store()
    db.row_factory = sqlite3.Row

    class Statement:
        def __init__(self, sql):
            self.sql = sql
            self.args = ()

        def bind(self, *args):
            self.args = args
            return self

        async def all(self):
            return {"results": [dict(row) for row in db.execute(self.sql, self.args).fetchall()]}

        async def run(self):
            result = db.execute(self.sql, self.args)
            db.commit()
            return {"meta": {"changes": result.rowcount}}

    class Database:
        def prepare(self, sql):
            return Statement(sql)

    calls = []

    async def handler(purchase, execution):
        calls.append((purchase, execution))
        return "provider-id"

    dispatcher = D1Dispatcher(Database(), "store", "test", {"email": handler})
    actions = (
        Action("first", ("one",), "email", "private/one", "a" * 64),
        Action("second", ("two",), "email", "private/two", "b" * 64),
    )
    assert asyncio.run(dispatcher.dispatch("order1", actions)) == {"first": "accepted", "second": "accepted"}
    assert asyncio.run(dispatcher.dispatch("order1", actions)) == {
        "first": "already_claimed",
        "second": "already_claimed",
    }
    assert [call[1].workflow_key for call in calls] == ["first", "second"]
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None


@pytest.mark.parametrize("fault", ["claim_response_lost", "acceptance_write_failed"])
def test_d1_ambiguous_write_never_blindly_resends(fault):
    import asyncio

    from cartray.post_purchase import D1Dispatcher

    db = setup_store()
    db.row_factory = sqlite3.Row
    state = {"fault": fault}

    class Statement:
        def __init__(self, sql):
            self.sql, self.args = sql, ()

        def bind(self, *args):
            self.args = args
            return self

        async def all(self):
            return {"results": [dict(row) for row in db.execute(self.sql, self.args).fetchall()]}

        async def run(self):
            if state["fault"] == "acceptance_write_failed" and "state='accepted'" in self.sql:
                state["fault"] = None
                raise TimeoutError("simulated write failure")
            result = db.execute(self.sql, self.args)
            db.commit()
            if state["fault"] == "claim_response_lost" and "INSERT OR IGNORE" in self.sql:
                state["fault"] = None
                raise TimeoutError("simulated lost claim response")
            return {"meta": {"changes": result.rowcount}}

    class Database:
        def prepare(self, sql):
            return Statement(sql)

    calls = []
    dispatcher = D1Dispatcher(Database(), "store", "test", {"email": lambda *args: calls.append(args) or "provider"})
    action = (Action("first", ("one",), "email", "config", "a" * 64),)
    if fault == "claim_response_lost":
        with pytest.raises(TimeoutError):
            asyncio.run(dispatcher.dispatch("order1", action))
        assert len(calls) == 0
    else:
        assert asyncio.run(dispatcher.dispatch("order1", action)) == {"first": "uncertain"}
        assert len(calls) == 1
    expected = "pending_claim" if fault == "claim_response_lost" else "already_claimed"
    assert asyncio.run(dispatcher.dispatch("order1", action)) == {"first": expected}
    assert len(calls) == (0 if fault == "claim_response_lost" else 1)


def test_enabled_configuration_rejects_empty_actions():
    from cartray.post_purchase import parse_action_config

    with pytest.raises(ValueError, match="invalid post-purchase configuration"):
        parse_action_config('{"merchant":"store","environment":"test","actions":[]}')
