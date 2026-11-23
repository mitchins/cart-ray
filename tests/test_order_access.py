import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from kinglet import MockD1Database

from cartray.order_access import (
    issue_order_access,
    issuer_token,
    purchased_resources,
    resolve_configured_resource,
    validate_resource_configuration,
)


@pytest.fixture
def db():
    database = MockD1Database()
    for path in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
        database.conn.executescript(path.read_text())
    database.conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", (
        "order-1", "request-1", "fingerprint", "catalogue", "digest", "aud", 100, 1,
    ))
    database.conn.commit()
    yield database
    database.conn.close()


def run(value):
    return asyncio.run(value)


def test_confirmed_order_has_one_reusable_access_for_mixed_resources(db):
    conn = db.conn
    conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)",
                 ("order-1", "A", 2, "price_a", 40, json.dumps(["course/week-1", "external"])) )
    conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)",
                 ("order-1", "B", 1, "price_b", 20, json.dumps(["external2"])) )
    conn.execute("INSERT INTO checkout_sessions(order_id,state,projection_nonce,updated_at) VALUES (?,?,?,?)",
                 ("order-1", "creating", "nonce", 1))
    conn.execute("INSERT INTO post_purchase_installation VALUES (1,?,?)", ("merchant", "test"))
    key = "x" * 32
    with pytest.raises(ValueError, match="unavailable"):
        run(issue_order_access(db, key, "merchant", "test", "order-1"))
    conn.execute("UPDATE checkout_sessions SET settlement_state='confirmed' WHERE order_id='order-1'")
    conn.execute("INSERT INTO outbox(order_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                 ("order-1", "OrderConfirmed", "{}", 2))
    token = run(issue_order_access(db, key, "merchant", "test", "order-1"))
    assert run(issue_order_access(db, key, "merchant", "test", "order-1")) == token
    with pytest.raises(ValueError, match="unavailable"):
        run(issue_order_access(db, key, "other-merchant", "test", "order-1"))
    assert len(token) == 43 and token not in str(conn.execute("SELECT * FROM order_access").fetchall())
    assert run(purchased_resources(db, token, "merchant", "test")) == [
        {"product_key": "A", "quantity": 2, "resources": ["course/week-1", "external"]},
        {"product_key": "B", "quantity": 1, "resources": ["external2"]},
    ]
    async def get_object(key):
        return SimpleNamespace(body=b"private") if key == "private/file" else None
    resources = {"course/week-1": {"provider": "private", "binding": "FILES", "key": "private/file"},
                 "external": {"provider": "external", "url": "https://example.test/resource"},
                 "external2": {"provider": "external", "url": "https://example.test/another"},
                 "not-purchased": {"provider": "external", "url": "https://example.test/secret"}}
    bindings = SimpleNamespace(FILES=SimpleNamespace(get=get_object))
    file = run(resolve_configured_resource("course/week-1", resources, bindings))
    assert file["status"] == "file" and file["body"] == b"private" and file["filename"] == "download"
    assert run(resolve_configured_resource("external", resources, bindings))["url"] == "https://example.test/resource"
    with pytest.raises(ValueError, match="unavailable"):
        run(purchased_resources(db, token, "other-merchant", "test"))
    conn.execute("UPDATE order_access SET revoked_at=3 WHERE order_id='order-1'")
    with pytest.raises(ValueError, match="unavailable"):
        run(purchased_resources(db, token, "merchant", "test"))
    with pytest.raises(ValueError, match="unavailable"):
        run(issue_order_access(db, key, "merchant", "test", "order-1"))


def test_order_page_and_download_reject_unpurchased_items(db):
    from kinglet import TestClient

    from cartray.worker import create_app

    conn = db.conn
    conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)",
                 ("order-1", "A", 2, "price_a", 40, json.dumps(["file", "external"])))
    conn.execute("INSERT INTO checkout_sessions(order_id,state,projection_nonce,updated_at) VALUES (?,?,?,?)",
                 ("order-1", "creating", "nonce", 1))
    conn.execute("UPDATE checkout_sessions SET settlement_state='confirmed' WHERE order_id='order-1'")
    conn.execute("INSERT INTO outbox(order_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                 ("order-1", "OrderConfirmed", "{}", 2))
    conn.execute("INSERT INTO post_purchase_installation VALUES (1,?,?)", ("second-merchant", "test"))
    async def allow(_):
        return {"success": True}

    async def get_object(key):
        return SimpleNamespace(body=b"dummy file") if key == "private/file" else None

    resources = {"file": {"provider": "private", "binding": "FILES", "key": "private/file",
                          "filename": "Example.pdf", "mime_type": "application/pdf"},
                 "external": {"provider": "external", "url": "https://example.test/resource",
                              "filename": "Example link"},
                 "not-purchased": {"provider": "external", "url": "https://example.test/secret"}}
    env = {"CARTRAY_ORDER_ACCESS_MODE": "test", "CARTRAY_ENVIRONMENT": "test",
           "CARTRAY_MERCHANT_ID": "second-merchant", "CARTRAY_SIGNING_KEY_ID": "test-install",
           "CARTRAY_ORDER_ACCESS_KEY": "z" * 32,
           "CARTRAY_RESOURCE_CONFIG": json.dumps(resources), "DB": db,
           "CHECKOUT_STATUS_RATE_LIMITER": SimpleNamespace(limit=allow),
           "FILES": SimpleNamespace(get=get_object)}
    client = TestClient(create_app(), env=env)
    payload = {"order_id": "order-1"}
    internal_headers = {"content-length": str(len(json.dumps(payload).encode())),
                        "authorization": "Bearer " + issuer_token("z" * 32)}
    assert client.request("POST", "/internal/order-access", json_data=payload)[0] == 404
    status, _, issued = client.request("POST", "/internal/order-access", json_data=payload, headers=internal_headers)
    assert status == 200
    path = json.loads(issued)["path"]
    token = path.split("token=", 1)[1]
    status, _, repeated = client.request("POST", "/internal/order-access", json_data=payload, headers=internal_headers)
    assert status == 200 and json.loads(repeated)["path"] == path
    headers = {"cf-connecting-ip": "192.0.2.1"}
    status, response_headers, body = client.request("GET", "/order-access", headers=headers)
    assert status == 200 and "Your order" in str(body)
    assert response_headers["Cache-Control"] == "no-store" and token not in str(body)
    data = {"token": token}
    json_headers = {**headers, "content-type": "application/json",
                    "content-length": str(len(json.dumps(data).encode()))}
    status, _, body = client.request("POST", "/order-access", json_data=data, headers=json_headers)
    assert status == 200 and "Example.pdf" in str(body) and "Example link" in str(body)
    assert "private/file" not in str(body) and "example.test/secret" not in str(body)

    def download(index):
        form = f"token={token}&resource={index}"
        return client.request("POST", "/order-access", data=form, headers={**headers,
                              "content-type": "application/x-www-form-urlencoded",
                              "content-length": str(len(form))})

    assert download("1.0")[0] == 404
    status, response_headers, _ = download("0.1")
    assert status == 302 and response_headers["Location"] == "https://example.test/resource"
    status, response_headers, body = download("0.0")
    assert status == 200 and response_headers["Content-Type"] == "application/pdf" and body == "b'dummy file'"
    assert client.request("GET", "/order-access?token=invalid", headers=headers)[0] == 404



def test_existing_checkout_settlement_produces_one_order_access_with_all_purchased_resources(db, fixture_catalogue):
    from cartray.canonical import CanonicalItem
    from cartray.checkout import CheckoutService
    from cartray.gateway import FakePaymentGateway
    from cartray.models import CheckoutRequest
    from cartray.store import D1OrderStore

    db.conn.execute("INSERT INTO post_purchase_installation VALUES (1,?,?)", ("new-store", "test"))
    db.conn.commit()
    gateway = FakePaymentGateway()
    store = D1OrderStore(db)
    service = CheckoutService(fixture_catalogue, store, gateway)
    request = CheckoutRequest("mixed-order", fixture_catalogue.version,
                              (CanonicalItem("TEST-BUNDLE", 1), CanonicalItem("TEST-SUPPORT-HOURS", 2)))
    redirect = run(service.checkout(request))
    order = gateway.requests[0].order_id
    with pytest.raises(ValueError, match="unavailable"):
        run(issue_order_access(db, "z" * 32, "new-store", "test", order))
    args = {"order_id": order, "session_id": redirect.session_id, "event_id": "evt_test_order",
            "payload": {"id": "evt_test_order"}, "payload_sha256": "sha256:test",
            "payment_status": "paid", "amount_total_minor": 100, "now": 100}
    assert run(store.confirm_settlement(**args)) is False
    assert run(store.confirm_settlement(**args)) is True
    token = run(issue_order_access(db, "z" * 32, "new-store", "test", order))
    items = run(purchased_resources(db, token, "new-store", "test"))
    assert items == [
        {"product_key": "TEST-BUNDLE", "quantity": 1, "resources": ["TEST-RESOURCE-A", "TEST-RESOURCE-B"]},
        {"product_key": "TEST-SUPPORT-HOURS", "quantity": 2, "resources": ["TEST-RESOURCE-SUPPORT-HOURS"]},
    ]
    assert run(issue_order_access(db, "z" * 32, "new-store", "test", order)) == token


def test_resource_configuration_rejects_invalid_sources_and_supports_missing_files():
    good = {"guide": {"provider": "private", "binding": "FILES", "key": "guide.pdf",
                      "filename": "Guide.pdf", "mime_type": "application/pdf"},
            "link": {"provider": "external", "url": "https://example.test/resource"}}
    validate_resource_configuration(good, {"FILES": "private-bucket"})
    for resources in (
        [],
        {"": good["guide"]},
        {"guide": {"provider": "unknown"}},
        {"guide": {"provider": "private", "binding": "OTHER", "key": "guide.pdf"}},
        {"guide": {"provider": "private", "binding": "FILES", "key": ""}},
        {"link": {"provider": "external", "url": "http://example.test/resource"}},
        {"link": {"provider": "external", "url": "https://user@example.test/secret"}},
        {"link": {"provider": "external", "url": "https://example.test/", "filename": "../secret"}},
        {"link": {"provider": "external", "url": "https://example.test/", "mime_type": "invalid"}},
    ):
        with pytest.raises(ValueError):
            validate_resource_configuration(resources, {"FILES": "private-bucket"})
    assert run(resolve_configured_resource("missing", good, SimpleNamespace())) == {"status": "unavailable"}
    assert run(resolve_configured_resource("guide", good, SimpleNamespace())) == {"status": "unavailable"}

    async def unavailable(_key):
        raise RuntimeError("provider unavailable")

    bindings = SimpleNamespace(FILES=SimpleNamespace(get=unavailable))
    assert run(resolve_configured_resource("guide", good, bindings)) == {"status": "unavailable"}


def test_order_access_rejects_short_keys_and_malformed_tokens(db):
    with pytest.raises(ValueError, match="key"):
        run(issue_order_access(db, "short", "merchant", "test", "order-1"))
    with pytest.raises(ValueError, match="scope"):
        run(issue_order_access(db, "x" * 32, "invalid merchant", "test", "order-1"))
    with pytest.raises(ValueError, match="capability"):
        run(purchased_resources(db, "invalid", "merchant", "test"))
