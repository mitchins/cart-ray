from __future__ import annotations

import sqlite3

from cartray.store import SCHEMA


def test_sqlite_schema_contains_the_current_d1_settlement_tables():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(checkout_sessions)")}
    assert {"settlement_state", "settlement_session_id", "settlement_event_id"} <= columns
    event_columns = {row[1] for row in connection.execute("PRAGMA table_info(stripe_events)")}
    assert {"payload_sha256", "processing_state"} <= event_columns
