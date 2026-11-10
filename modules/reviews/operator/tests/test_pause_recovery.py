"""Expired queue hints reconstruct from durable fenced facts without cursor rewind."""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from test_lifecycle import build_at, lifecycle
from test_operations import operations


def paused_with_orders(tmp_path, count=2):
    install, provider, database, _, _ = lifecycle(tmp_path)
    build_at(install, "a" * 40)
    install.deploy()
    install.activate(True)
    commerce = install.db("commerce")
    for identity in [1, *range(43, 43 + count)]:
        order = str(identity)
        database.query(commerce, "INSERT INTO orders VALUES (?,?,'fp','mv','digest','usd',100,1)", [order, order])
        database.query(
            commerce,
            "INSERT INTO outbox (id,order_id,event_type,payload_json,created_at) VALUES (?,?,'OrderConfirmed','{}',1)",
            [identity, order],
        )
    database.query(
        commerce,
        "INSERT INTO reviews_subscriber_checkpoint VALUES (?,?,?,?,?,?,?,?)",
        [
            install.config["namespace"],
            install.config["namespace"],
            commerce,
            "acct_Test",
            "catalogue",
            "mapping",
            42,
            42 + count,
        ],
    )
    install.pause()
    published = []
    request = provider.request

    def capture(path, method="GET", data=None):
        if path.endswith("/messages/batch"):
            for role in ("events", "schedule", "due"):
                identifier = install.state["resources"]["queue:" + role]["id"]
                assert provider.queue_settings[identifier]["delivery_paused"] is True
            published.extend(data["messages"])
            return {}
        return request(path, method, data)

    provider.request = capture
    return install, database, published


def test_expired_hints_recover_post_fence_signed_ids_without_rewinding_or_resetting_data(tmp_path):
    install, database, published = paused_with_orders(tmp_path)
    before = database.writes
    install.recover_paused_hints()
    assert [item["body"]["outbox_id"] for item in published] == [43, 44]
    key = bytes.fromhex(install.vault()["event_key"])
    for item in published:
        body = item["body"]
        assert item["content_type"] == "json"
        payload = [
            "cartray-reviews-order-confirmed-v1",
            body["issuer"],
            body["installation"],
            body["store"],
            "test",
            body["outbox_id"],
        ]
        assert (
            body["signature"]
            == hmac.new(key, json.dumps(payload, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        )
    assert database.writes == before
    checkpoint = database.query(
        install.db("commerce"), "SELECT activation_watermark,last_outbox_id FROM reviews_subscriber_checkpoint"
    )
    assert checkpoint == [{"activation_watermark": 42, "last_outbox_id": 44}]
    assert install.state["pause_recovery"]["complete"] is True
    assert install.activate(True)["phase"] == "active"


def test_large_recovery_is_bounded_and_resumes_or_restarts_expired_partial_replay(tmp_path):
    install, _, published = paused_with_orders(tmp_path, 401)
    with patch.object(operations.time, "time", return_value=1000):
        with pytest.raises(ValueError, match="page budget"):
            install.recover_paused_hints()
        assert len(published) == 400
        install.recover_paused_hints()
        assert len(published) == 401
    # Queues can expire even these reconstructed messages during a prolonged pause.
    # A stale recovery cursor must never authorize resume without a new full replay.
    published.clear()
    with patch.object(operations.time, "time", return_value=1301):
        with pytest.raises(ValueError, match="page budget"):
            install.recover_paused_hints()
    assert len(published) == 400
    assert published[0]["body"]["outbox_id"] == 43
