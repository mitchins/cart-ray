from __future__ import annotations

import json
import runpy
import shlex
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "m10a_acceptance.py"


def test_prepare_disables_before_creating_two_free_cart_ray_sessions(tmp_path):
    module = runpy.run_path(str(SCRIPT))
    calls = []

    def request(method, url, headers, body):
        calls.append((method, url, headers, body))
        if url.endswith("/disable"):
            return {"id": "we_test_destination", "livemode": False, "status": "disabled"}
        if url.endswith("/catalogue"):
            return {
                "version": "sha256:catalogue",
                "products": [{"product_key": "TEST-FREE", "amount_minor": 0, "max_quantity": 1}],
            }
        if url.endswith("/checkout"):
            return {
                "session_id": f"cs_test_{len(calls)}",
                "checkout_url": f"https://checkout.stripe.com/c/pay/cs_test_{len(calls)}",
            }
        raise AssertionError(url)

    state_file = tmp_path / "state.json"
    state = module["prepare"](
        key="rk_test_fixture",
        destination_id="we_test_destination",
        api_base_url="https://cartray.test",
        state_file=state_file,
        request_json=request,
    )

    assert [url.rsplit("/", 1)[-1] for _, url, _, _ in calls] == ["disable", "catalogue", "checkout", "checkout"]
    assert state["sessions"]["confirm"]["session_id"] == "cs_test_3"
    assert state["sessions"]["expire"]["session_id"] == "cs_test_4"
    assert state_file.stat().st_mode & 0o777 == 0o600


def test_prepare_refuses_an_existing_state_file_before_disabling_a_destination(tmp_path):
    module = runpy.run_path(str(SCRIPT))
    state_file = tmp_path / "state.json"
    state_file.write_text("{}\n", encoding="utf-8")

    with pytest.raises(module["AcceptanceError"], match="already exists"):
        module["prepare"](
            key="rk_test_fixture",
            destination_id="we_test_destination",
            api_base_url="https://cartray.test",
            state_file=state_file,
            request_json=lambda *_: pytest.fail("request must not occur"),
        )


def test_expire_targets_only_the_prepared_test_session():
    module = runpy.run_path(str(SCRIPT))
    state = {
        "schema": 1,
        "destination_id": "we_test_destination",
        "sessions": {
            "confirm": {"session_id": "cs_test_confirm", "checkout_url": "https://checkout.stripe.com/confirm"},
            "expire": {"session_id": "cs_test_expire", "checkout_url": "https://checkout.stripe.com/expire"},
        },
    }
    calls = []

    def request(method, url, headers, body):
        calls.append((method, url, headers, body))
        return {"id": "cs_test_expire", "livemode": False, "status": "expired"}

    result = module["expire"](key="rk_test_fixture", state=state, request_json=request)

    assert result["status"] == "expired"
    assert calls == [
        (
            "POST",
            "https://api.stripe.com/v1/checkout/sessions/cs_test_expire/expire",
            {"Authorization": "Bearer rk_test_fixture", "Stripe-Version": "2025-09-30.clover"},
            b"",
        )
    ]


def test_d1_commands_are_targeted_to_only_the_prepared_sessions():
    module = runpy.run_path(str(SCRIPT))
    state = {
        "schema": 1,
        "destination_id": "we_test_destination",
        "sessions": {
            "confirm": {"session_id": "cs_test_confirm", "checkout_url": "https://checkout.stripe.com/confirm"},
            "expire": {"session_id": "cs_test_expire", "checkout_url": "https://checkout.stripe.com/expire"},
        },
    }

    due = module["d1_command"](state)
    verify = module["verification_command"](state)

    assert "unixepoch() - 3700" in due
    assert "next_attempt_at = 0" in due
    assert "cs_test_confirm" in due and "cs_test_expire" in due
    assert "--remote" in due
    assert "checkout_reconciliations" in verify
    assert "stripe_event_count" in verify

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """CREATE TABLE checkout_sessions (
               order_id TEXT PRIMARY KEY,
               external_session_id TEXT,
               settlement_state TEXT,
               updated_at INTEGER
             );
             CREATE TABLE checkout_reconciliations (
               order_id TEXT PRIMARY KEY,
               session_id TEXT UNIQUE,
               next_attempt_at INTEGER,
               lease_token TEXT,
               lease_expires_at INTEGER,
               updated_at INTEGER
             );"""
    )
    connection.executemany(
        "INSERT INTO checkout_sessions VALUES (?, ?, 'pending', 99999999999)",
        (("order_confirm", "cs_test_confirm"), ("order_expire", "cs_test_expire")),
    )
    connection.executescript(shlex.split(due)[-1])
    rows = connection.execute(
        "SELECT session_id, next_attempt_at, lease_token FROM checkout_reconciliations ORDER BY session_id"
    ).fetchall()
    assert rows == [("cs_test_confirm", 0, None), ("cs_test_expire", 0, None)]


def test_state_rejects_wrong_schema_and_secret_errors_are_redacted(tmp_path):
    module = runpy.run_path(str(SCRIPT))
    state_file = tmp_path / "state.json"
    state_file.write_text('{"schema": 2}\n', encoding="utf-8")

    with pytest.raises(module["AcceptanceError"], match="unsupported schema"):
        module["_read_state"](state_file)

    state_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "destination_id": "we_test_destination",
                "sessions": {
                    "confirm": {"session_id": "cs_test_safe", "checkout_url": "https://checkout.stripe.com/safe"},
                    "expire": {
                        "session_id": "cs_test_bad'); DELETE FROM checkout_sessions; --",
                        "checkout_url": "https://checkout.stripe.com/bad",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module["AcceptanceError"], match="invalid Session ID"):
        module["_read_state"](state_file)

    assert module["_safe_error"](RuntimeError("rk_test_secret_must_not_escape")) == "[redacted]"
