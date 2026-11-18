import asyncio
import json
from pathlib import Path

import pytest
from kinglet import MockD1Database, TestClient

from cartray.canonical import CanonicalItem
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRequest
from cartray.store import D1OrderStore, SqliteOrderStore
from cartray.worker import create_app


@pytest.mark.parametrize("backend", ["sqlite", "d1"])
def test_receipt_is_an_allowlisted_immutable_order_snapshot(fixture_catalogue, backend):
    if backend == "d1":
        database = MockD1Database()
        for migration in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            database.conn.executescript(migration.read_text())
        store, connection = D1OrderStore(database), database.conn
    else:
        store = SqliteOrderStore.in_memory()
        connection = store.connection
    service = CheckoutService(fixture_catalogue, store, FakePaymentGateway())
    checkout = asyncio.run(service.checkout(CheckoutRequest(
        "receipt-order", fixture_catalogue.version,
        (CanonicalItem("TEST-TEMPLATE", 1), CanonicalItem("TEST-SUPPORT-HOURS", 3)),
    )))
    session_id = "cs_test_receipt"
    connection.execute("UPDATE checkout_sessions SET external_session_id = ? WHERE external_session_id = ?",
                       (session_id, checkout.session_id))

    async def factory(_env):
        return store

    client = TestClient(create_app(status_store_factory=factory),
                        env={"CARTRAY_STOREFRONT_ORIGIN": "https://store.test.invalid"})
    path = f"/checkout-status?session_id={session_id}"
    assert json.loads(client.request("GET", path)[2]) == {"state": "pending"}
    connection.execute("UPDATE checkout_sessions SET settlement_state = 'confirmed' WHERE external_session_id = ?",
                       (session_id,))
    connection.commit()
    status, headers, body = client.request("GET", path)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == {"state": "confirmed", "receipt": {"currency": "aud", "items": [
        {"product_key": "TEST-SUPPORT-HOURS", "quantity": 3, "unit_amount_minor": 10000},
        {"product_key": "TEST-TEMPLATE", "quantity": 1, "unit_amount_minor": 2500},
    ]}}
    # Another checkout must not change the receipt for the first session.
    later = asyncio.run(service.checkout(CheckoutRequest(
        "later-cart", fixture_catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),),
    )))
    assert client.request("GET", path)[2] == body
    connection.execute("UPDATE checkout_sessions SET settlement_state = 'expired', external_session_id = ? "
                       "WHERE external_session_id = ?", ("cs_test_expired", later.session_id))
    connection.commit()
    assert asyncio.run(store.checkout_receipt("cs_test_expired")) is None
    assert json.loads(client.request("GET", "/checkout-status?session_id=cs_test_expired")[2]) == {"state": "expired"}
