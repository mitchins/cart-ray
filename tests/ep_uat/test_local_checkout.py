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
