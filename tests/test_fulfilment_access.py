import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cartray.fulfilment import (
    ProviderRegistry,
    Redirect,
    Stream,
    Unavailable,
    authorised_access,
    external_link,
    private_object,
    token_digest,
)


class D1Query:
    def __init__(self, db, sql):
        self.db, self.sql = db, sql

    def bind(self, *values):
        self.values = values
        return self

    async def all(self):
        return {"results": [dict(row) for row in self.db.execute(self.sql, self.values)]}


class D1:
    def __init__(self, db):
        self.db = db

    def prepare(self, sql):
        return D1Query(self.db, sql)


class Bucket:
    async def get(self, key):
        return SimpleNamespace(body=b"dummy") if key == "private/file" else None


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


CONFIGS = (
    {"url": "https://example.test/p?id=1", "allowed_hosts": ["example.test"]},
    {"bucket_binding": "FILES", "key": "private/file"},
    {},
)


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "migrations").glob("*.sql")):
        db.executescript(path.read_text())
    db.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", ("order", "request", "hash", "version", "digest", "AUD", 100, 0)
    )
    db.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)", ("order", "offer", 1, "price", 100, "[]"))
    db.execute("INSERT INTO post_purchase_installation VALUES (1,?,?)", ("store", "test"))
    db.execute("INSERT INTO fulfilment_order_plans VALUES (?,?,?,?,?)", ("order", "store", "test", "{}", "0" * 64))
    for n, provider in enumerate(("link", "object", "synthetic")):
        db.execute(
            "INSERT INTO fulfilment_resources VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("store", "test", str(n), "v1", provider, str(n), digest(CONFIGS[n]), "file.pdf", "application/pdf", "{}"),
        )
        db.execute(
            "INSERT INTO fulfilment_order_bindings VALUES (?,?,?,?,?,?,?,?,?)",
            ("store", "test", "order", "workflow", "digest", "offer", str(n), "v1", n),
        )
    db.execute(
        "INSERT INTO checkout_sessions (order_id,state,projection_nonce,updated_at,settlement_state) "
        "VALUES ('order','creating','nonce',0,'confirmed')"
    )
    db.execute(
        "INSERT INTO outbox (order_id,event_type,payload_json,created_at) VALUES ('order','OrderConfirmed','{}',0)"
    )
    for n in range(3):
        db.execute(
            "INSERT INTO fulfilment_entitlements VALUES (?,?,?,?,?,?,?,?,?)",
            ("store", "test", "order", "workflow", "offer", n, 1, None, None),
        )
        db.execute(
            "INSERT INTO fulfilment_capabilities VALUES (?,?,?,?,?,?,?,?,?)",
            (token_digest(str(n) * 43), "store", "test", "order", "workflow", "offer", n, 1, None),
        )
    return db


def test_independent_adapters_and_repeated_non_consuming_access():
    db = database()

    async def synthetic(resource, config):
        return Stream(b"third-provider", resource.filename, resource.mime_type)

    registry = ProviderRegistry({"link": external_link, "object": private_object, "synthetic": synthetic})
    configs = {
        ("store", "0"): (CONFIGS[0], digest(CONFIGS[0])),
        ("store", "1"): ({"bucket": Bucket(), "key": "private/file"}, digest(CONFIGS[1])),
        ("store", "2"): ({}, digest(CONFIGS[2])),
    }

    def access(n):
        return asyncio.run(authorised_access(D1(db), str(n) * 43, registry, configs, 10))

    assert access(0) == Redirect("https://example.test/p?id=1")
    assert access(0) == Redirect("https://example.test/p?id=1")
    assert access(1) == Stream(b"dummy", "file.pdf", "application/pdf")
    assert access(2) == Stream(b"third-provider", "file.pdf", "application/pdf")
    assert isinstance(access(9), Unavailable)
    db.execute("UPDATE fulfilment_entitlements SET revoked_at=11 WHERE ordinal=1")
    assert isinstance(access(1), Unavailable)
    assert access(0) == Redirect("https://example.test/p?id=1")


def test_order_binding_is_sealed():
    db = database()
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        db.execute(
            "INSERT INTO fulfilment_order_bindings VALUES (?,?,?,?,?,?,?,?,?)",
            ("store", "test", "order", "other", "digest", "offer", "0", "v1", 0),
        )


def test_registered_third_provider_works_via_the_ordinary_http_route():
    import json

    from kinglet import TestClient

    from cartray.worker import create_app

    db = database()

    async def synthetic(resource, _config):
        return Stream(b"third-provider", resource.filename, resource.mime_type)

    registry = ProviderRegistry({"link": external_link, "object": private_object, "synthetic": synthetic})
    env = {
        "CARTRAY_FULFILMENT_MODE": "test",
        "CARTRAY_ENVIRONMENT": "test",
        "CARTRAY_FULFILMENT_PROVIDER_CONFIG": json.dumps({"merchant": "store", "resources": {"2": {}}}),
        "DB": D1(db),
    }
    client = TestClient(create_app(fulfilment_registry=registry), env=env)
    status, headers, body = client.request("GET", "/fulfilment/access?token=" + "2" * 43)
    assert status == 200
    assert body == "b'third-provider'"  # Kinglet TestClient stringifies byte bodies.
    assert headers["Cache-Control"] == "no-store"
    assert "file.pdf" in headers["Content-Disposition"]
    assert client.request("GET", "/fulfilment/access?token=" + "2" * 43)[0] == 200
    assert client.request("HEAD", "/fulfilment/access?token=" + "2" * 43)[0] == 200
    assert client.request("GET", "/fulfilment/access?token=invalid")[0] == 404
    assert client.request("GET", "/fulfilment/access?token=" + "9" * 43)[0] == 404
