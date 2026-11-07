"""The pinned key cannot be silently replaced or removed from a test install."""
import hashlib
import hmac
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "consent" / "tools" / "pin_suppression_key.py"
spec = importlib.util.spec_from_file_location("pin_suppression_key", MODULE)
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)


def test_registry_requires_explicit_version_and_is_immutable():
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "consent" / "migrations" / "0002_suppression_key_registry.sql").read_text())
    first = pin.statement(key="y" * 40, store="acorn-store", installation="install_test", version="key1")
    assert "y" * 40 not in first
    db.executescript(first)
    row = db.execute("SELECT key_version,key_check FROM review_suppression_key_registry").fetchone()
    assert row == ("key1", "1b1a79f29dd2adcda1813cf03915cf4136368f231b988d5136fec6998750a6bc")
    for sql in (
        "DELETE FROM review_suppression_key_registry",
        "UPDATE review_suppression_key_registry SET key_version='key2'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        db.executescript(pin.statement(key="z" * 40, store="acorn-store", installation="install_test", version="key2"))


def test_statement_rejects_unsafe_scope_or_short_key():
    with pytest.raises(ValueError):
        pin.statement(key="short", store="acorn-store", installation="install_test", version="key1")
    with pytest.raises(ValueError):
        pin.statement(key="y" * 40, store="acorn-store'--", installation="install_test", version="key1")


def test_populated_history_cannot_be_pinned_without_a_matching_authoritative_contact():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE review_automation_recipient_bindings"
        "(store_key,environment,binding_version,recipient_context_hash);"
        "CREATE TABLE review_recipient_suppressions(store_key,environment,recipient_context_hash);"
    )
    key = "y" * 40
    payload = json.dumps(["acorn-recipient-v2", "acorn-store", "test", "Client@example.com"], separators=(",", ":"))
    digest = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    db.execute("INSERT INTO review_recipient_suppressions VALUES (?,?,?)", ("acorn-store", "test", digest))
    for wrong_key, contacts in [(key, None), ("z" * 40, ["Client@example.com"]), (key, ["client@example.com"])]:
        with pytest.raises(ValueError):
            pin.verify_history(db=db, key=wrong_key, store="acorn-store", contacts=contacts)
    pin.verify_history(db=db, key=key, store="acorn-store", contacts=["Client@EXAMPLE.COM"])
    # One matching identity is not sufficient if other historic suppressions
    # were made under another key or cannot be matched to authoritative data.
    db.execute("INSERT INTO review_recipient_suppressions VALUES (?,?,?)", ("acorn-store", "test", "f" * 64))
    with pytest.raises(ValueError):
        pin.verify_history(db=db, key=key, store="acorn-store", contacts=["Client@example.com"])


def test_verified_empty_history_can_pin_without_contact():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE review_automation_recipient_bindings"
        "(store_key,environment,binding_version,recipient_context_hash);"
        "CREATE TABLE review_recipient_suppressions(store_key,environment,recipient_context_hash);"
    )
    pin.verify_history(db=db, key="y" * 40, store="acorn-store", contacts=None)


def test_versioned_binding_is_populated_history_not_a_fresh_install():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE review_automation_recipient_bindings"
        "(store_key,environment,binding_version,recipient_context_hash);"
        "CREATE TABLE review_recipient_suppressions(store_key,environment,recipient_context_hash);"
    )
    db.execute("INSERT INTO review_automation_recipient_bindings VALUES (?,?,?,?)",
               ("acorn-store", "test", "v2_key1", "f" * 64))
    with pytest.raises(ValueError):
        pin.verify_history(db=db, key="y" * 40, store="acorn-store", contacts=None)
