from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "examples/ep-uat"
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("ep_uat_local_server", ROOT / "test_server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.path.pop(0)


def test_confirmation_scoped_to_current_owned_request():
    lane = object.__new__(module.TestLane)
    lane.connection = sqlite3.connect(":memory:")
    lane.connection.row_factory = sqlite3.Row
    lane.connection.executescript("""
        CREATE TABLE orders(order_id TEXT, checkout_request_id TEXT);
        CREATE TABLE checkout_sessions(order_id TEXT, external_session_id TEXT, settlement_state TEXT);
        CREATE TABLE uat_order_owner(order_id TEXT, token_hash TEXT);
        CREATE TABLE uat_checkout_owner(request_id TEXT, token_hash TEXT);
        CREATE TABLE uat_grants(order_id TEXT, product_key TEXT, quantity INTEGER);
        CREATE TABLE checkout_reconciliations(order_id TEXT, next_attempt_at INTEGER, lease_expires_at INTEGER);
        CREATE TABLE order_items(order_id TEXT, product_key TEXT, quantity INTEGER, fulfilment_resources_json TEXT);
        CREATE TABLE outbox(order_id TEXT, event_type TEXT);
        INSERT INTO orders VALUES ('old', 'old-request-12345678'), ('new', 'new-request-12345678');
        INSERT INTO checkout_sessions VALUES ('old', 'sess-old', 'confirmed'), ('new', NULL, 'pending');
        INSERT INTO uat_order_owner VALUES ('old', 'owner'), ('new', 'owner');
        INSERT INTO uat_checkout_owner VALUES ('old-request-12345678', 'owner'), ('new-request-12345678', 'owner');
        INSERT INTO uat_grants VALUES ('old', 'product', 1);
    """)
    lane.reverse = {"product": {"source_id": 171, "source_key": "ep:171"}}
    lane.followups = {"items": []}
    assert lane.grants("owner", "old-request-12345678")[0]["source_id"] == 171
    result = lane.reconcile("owner", "new-request-12345678")
    assert result == {"state": "pending", "grants": []}
    with pytest.raises(ValueError, match="not bound"):
        lane.reconcile("different-owner", "new-request-12345678")


def test_lost_checkout_response_cannot_be_claimed_by_another_browser():
    from types import SimpleNamespace

    class UncertainCheckout:
        async def checkout(self, request):
            raise TimeoutError("provider outcome unknown")

    lane = object.__new__(module.TestLane)
    lane.connection = sqlite3.connect(":memory:")
    lane.connection.row_factory = sqlite3.Row
    lane.connection.execute("CREATE TABLE uat_checkout_owner(request_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL)")
    lane.mapping = {"171": {"cartray_product_key": "EP-UAT-171"}}
    lane.catalogue = SimpleNamespace(version="fixture-v1")
    lane.checkout = UncertainCheckout()
    payload = {"request_id": "request-1234567890-abcd", "items": [{"source_id": 171, "quantity": 1}]}
    with pytest.raises(TimeoutError):
        lane.start("owner-one", payload)
    with pytest.raises(ValueError, match="already bound"):
        lane.start("owner-two", payload)
    with pytest.raises(TimeoutError):
        lane.start("owner-one", payload)


def test_keychain_secret_is_loaded_without_exposing_subprocess_output(monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="secret-value\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.read_secret(None, "cartray-test", tmp_path) == "secret-value"
    assert calls[0][0] == ["security", "find-generic-password", "-s", "cartray-test", "-w"]
    assert calls[0][1]["capture_output"] is True


def test_file_secret_requires_private_mode_and_outside_site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    key = tmp_path / "key"
    key.write_text("not-a-real-key")
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        module.read_secret(key, None, site)
    key.chmod(0o600)
    assert module.read_secret(key, None, site) == "not-a-real-key"
    inside = site / "key"
    inside.write_text("not-a-real-key")
    inside.chmod(0o600)
    with pytest.raises(ValueError, match="private"):
        module.read_secret(inside, None, site)


def test_merchant_workflow_instances_dispatch_separately_for_shared_link(monkeypatch, tmp_path):
    from cartray.store import SqliteOrderStore

    path = tmp_path / "uat-order-test.sqlite"
    db = sqlite3.connect(path)
    SqliteOrderStore(db)
    # Create only the immutable facts normally written by CartRay checkout.
    db.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", ("order", "request", "fingerprint", "v", "digest", "aud", 1, 1)
    )
    db.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)", ("order", "product", 1, "price", 1, "[]"))
    db.execute(
        "INSERT INTO checkout_sessions (order_id,state,external_session_id,projection_nonce,"
        "updated_at,settlement_state) "
        "VALUES (?,?,?,?,?,?)",
        ("order", "redirect_issued", "session", "nonce", 1, "confirmed"),
    )
    db.execute(
        "INSERT INTO outbox(order_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
        ("order", "OrderConfirmed", "{}", 1),
    )
    migration = Path(__file__).resolve().parents[2] / "migrations/0006_post_purchase_actions.sql"
    db.executescript(migration.read_text())
    db.execute("INSERT INTO post_purchase_installation VALUES (1,'effective-policy-uat','test')")
    db.execute("CREATE TABLE uat_delivery_emails(order_id TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    delivered = []

    class FakeDelivery:
        def __init__(self, *args):
            pass

        def send_verified(self, session_id, links):
            delivered.append((session_id, links))
            return f"id-{len(delivered)}"

    monkeypatch.setattr(module, "TestDelivery", FakeDelivery)
    lane = object.__new__(module.TestLane)
    lane.fixture = tmp_path
    lane.client = object()
    lane.test_delivery_config = ("test", "sender", "recipient")
    lane.mapping = {"1": {"source_key": "source", "cartray_product_key": "product"}}
    lane.followups = {
        "items": [
            {"source_key": "source", "workflow_key": "first", "dummy_url": "same"},
            {"source_key": "source", "workflow_key": "second", "dummy_url": "same"},
        ]
    }
    lane.send_test_delivery("order")
    lane.send_test_delivery("order")
    assert delivered == [("session", ["same"]), ("session", ["same"])]
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM post_purchase_executions WHERE state='accepted'").fetchone()[0] == 2


def test_background_selection_handles_due_orders_partial_workflows_and_missing_owner(monkeypatch, tmp_path):
    path = tmp_path / "uat-order-test.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY, checkout_request_id TEXT, created_at INTEGER);
            CREATE TABLE checkout_sessions(order_id TEXT, external_session_id TEXT, settlement_state TEXT);
            CREATE TABLE uat_order_owner(order_id TEXT PRIMARY KEY, token_hash TEXT);
            CREATE TABLE uat_checkout_owner(request_id TEXT PRIMARY KEY, token_hash TEXT);
            CREATE TABLE checkout_reconciliations(order_id TEXT, next_attempt_at INTEGER, lease_expires_at INTEGER);
            CREATE TABLE uat_delivery_emails(order_id TEXT);
            CREATE TABLE post_purchase_executions(order_id TEXT, workflow_key TEXT);
            CREATE TABLE order_items(order_id TEXT, product_key TEXT);
        """)
        for i in range(10):
            db.execute("INSERT INTO orders VALUES (?,?,?)", (f"old{i}", f"request{i}", i))
            db.execute("INSERT INTO checkout_sessions VALUES (?,?,?)", (f"old{i}", "session", "pending"))
            db.execute("INSERT INTO uat_checkout_owner VALUES (?,?)", (f"request{i}", "owner"))
            db.execute("INSERT INTO checkout_reconciliations VALUES (?,?,?)", (f"old{i}", 9999999999, None))
        for order, state in [("paid", "pending"), ("partial", "confirmed")]:
            db.execute("INSERT INTO orders VALUES (?,?,?)", (order, order, 20))
            db.execute("INSERT INTO checkout_sessions VALUES (?,?,?)", (order, "session", state))
            db.execute("INSERT INTO uat_checkout_owner VALUES (?,?)", (order, "owner"))
            db.execute("INSERT INTO order_items VALUES (?,?)", (order, "product"))
        db.execute("INSERT INTO post_purchase_executions VALUES ('partial','first')")

    def make_store(connection):
        connection.row_factory = sqlite3.Row
        return object()

    called = []
    monkeypatch.setattr(module, "SqliteOrderStore", make_store)
    monkeypatch.setattr(
        module.TestLane,
        "reconcile",
        lambda self, owner, request, origin="browser": called.append((owner, request, origin)),
    )
    lane = object.__new__(module.TestLane)
    lane.fixture, lane.client, lane.crypto = tmp_path, object(), object()
    lane.mapping = {"1": {"source_key": "source", "cartray_product_key": "product"}}
    lane.followups = {
        "items": [
            {"source_key": "source", "workflow_key": "first"},
            {"source_key": "source", "workflow_key": "second"},
        ]
    }
    lane.run_test_automation_once()
    assert sorted(called) == [("owner", "paid", "background"), ("owner", "partial", "background")]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM uat_order_owner").fetchone()[0] == 2


def test_native_checkout_adapter_accepts_only_authorised_offer_and_version():
    from types import SimpleNamespace

    lane = object.__new__(module.TestLane)
    lane.catalogue = SimpleNamespace(version="verified-v1")
    lane.reverse = {"EP-UAT-171": {"source_id": 171}}
    calls = []

    def start(owner, payload, *, native=False):
        assert native is True
        calls.append((owner, payload))
        return {"url": "https://checkout.stripe.com/test", "session_id": "cs_test_1"}

    lane.start = start
    payload = {
        "checkout_request_id": "request-1234567890",
        "manifest_version": "verified-v1",
        "items": [{"product_key": "EP-UAT-171", "quantity": 1}],
    }
    assert lane.start_native("owner", payload) == {
        "checkout_url": "https://checkout.stripe.com/test",
        "session_id": "cs_test_1",
    }
    assert calls[0][1]["items"] == [{"source_id": 171, "quantity": 1}]
    with pytest.raises(ValueError, match="version mismatch"):
        lane.start_native("owner", {**payload, "manifest_version": "unverified"})
    with pytest.raises(ValueError, match="not enabled"):
        lane.start_native("owner", {**payload, "items": [{"product_key": "unmapped", "quantity": 1}]})
    with pytest.raises(ValueError, match="invalid checkout request"):
        lane.start_native("owner", {**payload, "amount_minor": 1})
    assert len(calls) == 1


def test_current_grants_are_bound_to_the_confirmed_order_and_owner():
    lane = object.__new__(module.TestLane)
    lane.connection = sqlite3.connect(":memory:")
    lane.connection.row_factory = sqlite3.Row
    lane.connection.executescript("""
      CREATE TABLE orders(order_id TEXT,checkout_request_id TEXT);
      CREATE TABLE checkout_sessions(order_id TEXT,external_session_id TEXT,settlement_state TEXT);
      CREATE TABLE uat_order_owner(order_id TEXT,token_hash TEXT);
      CREATE TABLE uat_grants(order_id TEXT,product_key TEXT,quantity INTEGER);
      INSERT INTO orders VALUES ('one','request-one'),('two','request-two');
      INSERT INTO checkout_sessions VALUES ('one','cs_test_one','confirmed'),('two','cs_test_two','confirmed');
      INSERT INTO uat_order_owner VALUES ('one','owner'),('two','owner');
      INSERT INTO uat_grants VALUES ('one','p1',1),('two','p2',1);
    """)
    lane.reverse = {"p1": {"source_id": 1, "source_key": "one"}, "p2": {"source_id": 2, "source_key": "two"}}
    lane.followups = {"items": []}
    assert [grant["source_id"] for grant in lane.current_grants("owner", "cs_test_one")] == [1]
    with pytest.raises(ValueError, match="not bound"):
        lane.current_grants("other-owner", "cs_test_one")
    with pytest.raises(ValueError, match="not bound"):
        lane.current_grants("owner", "cs_test_unknown")
