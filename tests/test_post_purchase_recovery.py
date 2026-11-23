import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "post_purchase_recovery", Path(__file__).resolve().parents[1] / "scripts/post_purchase_recovery.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["recovery", *args])
    calls = []
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    recovery.main()
    return calls


def test_recovery_requires_test_scope_and_explicit_paused_operator(monkeypatch):
    scope = ["--account", "a" * 32, "--database", "merchant-test", "--merchant", "merchant"]
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "hold", *scope)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "inspect", "--account", "a" * 32, "--database", "merchant-prod", "--merchant", "merchant")
    calls = invoke(monkeypatch, "inspect", *scope)
    assert "state IN ('claimed','uncertain')" in calls[0][0][0][-1]
    assert "next_event_id" in calls[0][0][0][-1]
    assert "CASE state WHEN 'claimed' THEN 0" in calls[0][0][0][-1]
    calls = invoke(monkeypatch, "inspect", *scope, "--offset", "100")
    assert "LIMIT 100 OFFSET 100" in calls[0][0][0][-1]
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "inspect", *scope, "--offset", "-1")


def test_hold_quotes_evidence_and_binds_inspected_digest(monkeypatch):
    calls = invoke(
        monkeypatch, "hold", "--account", "a" * 32, "--database", "merchant-test", "--merchant", "merchant",
        "--order", "order-one", "--workflow", "flow-one", "--digest", "b" * 64, "--operator", "staff",
        "--evidence", "provider's receipt unknown", "--confirm-worker-paused",
    )
    command = calls[0][0][0]
    assert "--remote" in command
    assert "provider''s receipt unknown" in command[-1]
    assert "b" * 64 in command[-1]
    assert calls[0][1]["env"]["CLOUDFLARE_ACCOUNT_ID"] == "a" * 32


def test_many_uncertain_rows_do_not_hide_claimed_executions(monkeypatch):
    calls = invoke(monkeypatch, "inspect", "--account", "a" * 32, "--database", "merchant-test",
                   "--merchant", "merchant")
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE post_purchase_executions (
      merchant_id TEXT, environment TEXT, order_id TEXT, workflow_key TEXT, action_digest TEXT,
      state TEXT, provider_reference TEXT, created_at INTEGER, updated_at INTEGER)""")
    db.executemany("INSERT INTO post_purchase_executions VALUES ('merchant','test',?,'flow','digest',?,NULL,0,?)",
                   [(f"old-{n}", "uncertain", n) for n in range(100)] + [("blocked", "claimed", 1000)])
    db.execute("CREATE TABLE post_purchase_checkpoint (merchant_id TEXT, environment TEXT, "
               "last_outbox_id INTEGER, activation_watermark INTEGER)")
    db.execute("CREATE TABLE outbox (id INTEGER, order_id TEXT, event_type TEXT)")
    db.execute("CREATE TABLE orders (order_id TEXT)")
    db.execute("CREATE TABLE checkout_sessions (order_id TEXT)")
    db.execute("INSERT INTO post_purchase_checkpoint VALUES ('merchant','test',4,0)")
    db.execute("INSERT INTO orders VALUES ('blocked')")
    db.execute("INSERT INTO checkout_sessions VALUES ('blocked')")
    db.execute("INSERT INTO outbox VALUES (5,'blocked','OrderConfirmed')")
    checkpoint_query = calls[0][0][0][-1].split(";", 1)[0]
    assert db.execute(checkpoint_query).fetchone() == (4, 0, 5, "blocked")
    claim_query = calls[0][0][0][-1].split(";", 1)[1]
    assert db.execute(claim_query).fetchone()[0] == "blocked"


def test_shared_inspection_includes_native_recovery_claims_without_capabilities(monkeypatch):
    calls = invoke(monkeypatch, "inspect", "--account", "a"*32, "--database", "merchant-test",
                   "--merchant", "merchant", "--include-review-followups")
    query = calls[0][0][0][-1].split(';')[3]
    db = sqlite3.connect(':memory:')
    db.executescript("CREATE TABLE invitation_generations (store_key TEXT,environment TEXT,acquisition_id TEXT,"
                     "state TEXT,generation_id TEXT,delivery_identity TEXT,prepared_at INTEGER,token_hash TEXT);"
                     "CREATE TABLE order_review_messages (merchant_id TEXT,environment TEXT,order_id TEXT);")
    db.execute("INSERT INTO invitation_generations VALUES ('merchant','test','order','dispatching',?,?,1,?)",
               ('b'*64,'c'*64,'private-token-hash'))
    db.execute("INSERT INTO order_review_messages VALUES ('merchant','test','order')")
    row = db.execute(query).fetchone()
    assert row == ('order','review-recovery','dispatching','b'*64,'c'*64,1)
    assert 'token_hash' not in query and 'recipient' not in query
