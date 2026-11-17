from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ep_uat_delivery_email", Path(__file__).resolve().parents[2] / "examples/ep-uat/delivery_email.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Stripe:
    def __init__(self, **changes):
        self.changes = changes

    async def get(self, path, query):
        return {
            "id": "cs_test_1",
            "livemode": False,
            "payment_status": "paid",
            "customer_details": {"email": "test-1034@mitchellcurrie.com"},
            **self.changes,
        }


def make(stripe=None, transport=None):
    db = sqlite3.connect(":memory:")
    return module.TestDelivery(
        db, stripe or Stripe(), "api-test", "uat@mitchellcurrie.com", "test-1034@mitchellcurrie.com", transport
    )


def test_paid_order_sends_once_and_retry_keeps_original_claim():
    calls = []

    def send(body):
        calls.append(body)
        return {"data": {"succeeded": 1, "failed": 0, "email_id": "provider-id"}}

    delivery = make(transport=send)
    assert delivery.dispatch("order", "cs_test_1", ["https://www.dropbox.com/t/dummy"]) == "accepted"
    assert delivery.dispatch("order", "cs_test_1", ["https://www.dropbox.com/t/dummy"]) == "already_claimed"
    assert len(calls) == 1
    assert delivery.db.execute("SELECT state,provider_id FROM uat_delivery_emails").fetchone() == (
        "accepted",
        "provider-id",
    )
    assert calls[0]["to"] == ["test-1034@mitchellcurrie.com"]


@pytest.mark.parametrize(
    "changes",
    [
        {"payment_status": "unpaid"},
        {"livemode": True},
        {"id": "cs_test_other"},
        {"customer_details": {"email": "other@example.com"}},
    ],
)
def test_invalid_purchase_or_recipient_never_sends(changes):
    delivery = make(Stripe(**changes), lambda _: pytest.fail("must not send"))
    assert delivery.dispatch("order", "cs_test_1", ["dummy"]) == "recipient_or_payment_unverified"
    assert delivery.db.execute("SELECT COUNT(*) FROM uat_delivery_emails").fetchone()[0] == 0


def test_uncertain_delivery_is_not_automatically_resent():
    calls = []

    def send(body):
        calls.append(body)
        raise TimeoutError()

    delivery = make(transport=send)
    assert delivery.dispatch("order", "cs_test_1", ["dummy"]) == "uncertain"
    assert delivery.dispatch("order", "cs_test_1", ["dummy"]) == "already_claimed"
    assert len(calls) == 1
    assert delivery.db.execute("SELECT state FROM uat_delivery_emails").fetchone()[0] == "uncertain"


def test_no_link_means_no_provider_calls():
    delivery = make(transport=lambda _: pytest.fail("must not send"))
    assert delivery.dispatch("order", "cs_test_1", []) == "no_followup"


def test_concurrent_retries_claim_one_email(tmp_path):
    import concurrent.futures
    import threading

    path = tmp_path / "email.sqlite"
    barrier = threading.Barrier(2)
    calls = []

    class RacingStripe(Stripe):
        async def get(self, path, query):
            barrier.wait(timeout=5)
            return await super().get(path, query)

    def send(body):
        calls.append(body)
        return {"data": {"succeeded": 1, "failed": 0, "email_id": "provider-id"}}

    with sqlite3.connect(path) as db:
        module.TestDelivery(db, Stripe(), "api-test", "uat@mitchellcurrie.com", "test-1034@mitchellcurrie.com", send)

    def run():
        db = sqlite3.connect(path, timeout=5)
        try:
            delivery = module.TestDelivery(
                db, RacingStripe(), "api-test", "uat@mitchellcurrie.com", "test-1034@mitchellcurrie.com", send
            )
            return delivery.dispatch("order", "cs_test_1", ["dummy"])
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ["accepted", "already_claimed"]
    assert len(calls) == 1
