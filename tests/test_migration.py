import sqlite3
from pathlib import Path

import pytest


def test_d1_migration_preserves_immutable_and_idempotent_boundaries():
    migration = Path(__file__).parents[1] / "migrations" / "0001_commerce_kernel.sql"
    connection = sqlite3.connect(":memory:")
    connection.executescript(migration.read_text())
    connection.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("cr_1", "request_1", "digest_1", "catalogue_1", "items_1", "aud", 2500, 1),
    )
    connection.execute(
        "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)",
        ("cr_1", "TEST-TEMPLATE", 1, "price_test", 2500, "[]"),
    )
    connection.execute(
        "INSERT INTO checkout_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("cr_1", "creating", None, None, "nonce_1", 2, 1, 1),
    )
    connection.execute(
        "INSERT INTO stripe_events (stripe_event_id, event_type, payload_json, received_at) VALUES (?, ?, ?, ?)",
        ("evt_1", "checkout.session.completed", "{}", 1),
    )

    with pytest.raises(sqlite3.IntegrityError, match="orders are immutable"):
        connection.execute("UPDATE orders SET currency = 'usd' WHERE order_id = 'cr_1'")
    with pytest.raises(sqlite3.IntegrityError, match="order_items are immutable"):
        connection.execute(
            "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)",
            ("cr_1", "TEST-BUNDLE", 1, "price_bundle", 5000, "[]"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO stripe_events (stripe_event_id, event_type, payload_json, received_at) VALUES (?, ?, ?, ?)",
            ("evt_1", "checkout.session.completed", "{}", 2),
        )
