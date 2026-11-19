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
    prepare_notification,
    private_object,
    token_digest,
)
from cartray.post_purchase import Action, _action_digest


class D1Query:
    def __init__(self, db, sql):
        self.db, self.sql = db, sql

    def bind(self, *values):
        self.values = values
        return self

    async def all(self):
        return {"results": [dict(row) for row in self.db.execute(self.sql, self.values)]}

    async def run(self):
        rowcount = self.db.execute(self.sql, self.values).rowcount
        self.db.commit()
        return {"meta": {"changes": rowcount}}


class D1:
    def __init__(self, db):
        self.db = db

    def prepare(self, sql):
        return D1Query(self.db, sql)

    async def batch(self, statements):
        self.db.commit()
        try:
            self.db.execute("BEGIN")
            result = [{"meta": {"changes": self.db.execute(stmt.sql, stmt.values).rowcount}} for stmt in statements]
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise


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


def database(two_workflows=False, prepopulate=True):
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
    actions = {
        "merchant": "store",
        "environment": "test",
        "actions": [
            {
                "workflow_key": "workflow",
                "offer_keys": ["offer"],
                "handler": "service",
                "config_ref": "private/config",
                "config_digest": "a" * 64,
            }
        ],
    }
    if two_workflows:
        actions["actions"].append(
            {
                "workflow_key": "second",
                "offer_keys": ["offer"],
                "handler": "service",
                "config_ref": "private/second",
                "config_digest": "b" * 64,
            }
        )
    raw = json.dumps(actions, sort_keys=True, separators=(",", ":"))
    db.execute(
        "INSERT INTO fulfilment_order_plans VALUES (?,?,?,?,?)",
        ("order", "store", "test", raw, hashlib.sha256(raw.encode()).hexdigest()),
    )
    for n, provider in enumerate(("link", "object", "synthetic")):
        db.execute(
            "INSERT INTO fulfilment_resources VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "store",
                "test",
                str(n),
                "v1",
                provider,
                str(n),
                digest(CONFIGS[n]),
                "file.pdf",
                "application/pdf",
                json.dumps({"expires_at": None}),
            ),
        )
        db.execute(
            "INSERT INTO fulfilment_order_bindings VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "store",
                "test",
                "order",
                "workflow",
                _action_digest(Action("workflow", ("offer",), "service", "private/config", "a" * 64)),
                "offer",
                str(n),
                "v1",
                n,
            ),
        )
    if two_workflows:
        second = Action("second", ("offer",), "service", "private/second", "b" * 64)
        db.execute(
            "INSERT INTO fulfilment_order_bindings VALUES (?,?,?,?,?,?,?,?,?)",
            ("store", "test", "order", "second", _action_digest(second), "offer", "0", "v1", 0),
        )
    db.execute(
        "INSERT INTO checkout_sessions (order_id,state,projection_nonce,updated_at,settlement_state) "
        "VALUES ('order','creating','nonce',0,'confirmed')"
    )
    db.execute(
        "INSERT INTO outbox (order_id,event_type,payload_json,created_at) VALUES ('order','OrderConfirmed','{}',0)"
    )
    for n in range(3) if prepopulate else ():
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

    class Limiter:
        async def limit(self, _key):
            return {"success": True}

    env = {
        "CARTRAY_SIGNING_KEY_ID": "test-installation",
        "FULFILMENT_RATE_LIMITER": Limiter(),
        "CARTRAY_FULFILMENT_MODE": "test",
        "CARTRAY_ENVIRONMENT": "test",
        "CARTRAY_FULFILMENT_PROVIDER_CONFIG": json.dumps({"merchant": "store", "resources": {"2": {}}}),
        "DB": D1(db),
    }
    client = TestClient(create_app(fulfilment_registry=registry), env=env)
    original_request = client.request
    client.request = lambda method, path: original_request(method, path, headers={"cf-connecting-ip": "192.0.2.1"})
    status, headers, body = client.request("GET", "/fulfilment/access?token=" + "2" * 43)
    assert status == 200
    assert body == "b'third-provider'"  # Kinglet TestClient stringifies byte bodies.
    assert headers["Cache-Control"] == "no-store"
    assert "file.pdf" in headers["Content-Disposition"]
    assert client.request("GET", "/fulfilment/access?token=" + "2" * 43)[0] == 200
    assert client.request("HEAD", "/fulfilment/access?token=" + "2" * 43)[0] == 200
    assert client.request("GET", "/fulfilment/access?token=invalid")[0] == 404
    assert client.request("GET", "/fulfilment/access?token=" + "9" * 43)[0] == 404

    class Denied:
        async def limit(self, _key):
            return {"success": False}

    denied_env = {**env, "FULFILMENT_RATE_LIMITER": Denied(), "DB": None}
    denied = TestClient(create_app(fulfilment_registry=registry), env=denied_env)
    assert (
        denied.request("GET", "/fulfilment/access?token=" + "2" * 43, headers={"cf-connecting-ip": "192.0.2.1"})[0]
        == 429
    )


def test_pausing_delivery_does_not_disable_checkout_configuration():
    from cartray.worker import _fulfilment_checkout_config

    env = SimpleNamespace(CARTRAY_FULFILMENT_MODE="test", CARTRAY_ENVIRONMENT="test",
                          CARTRAY_POST_PURCHASE_MODE="disabled")
    assert _fulfilment_checkout_config(env) is None


def test_claim_prepares_multiple_grants_atomically_without_raw_capability_storage():
    db = database(prepopulate=False)
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    created = asyncio.run(prepare_notification(D1(db), "store", "test", "order", action, 100))
    assert created is not None
    nonce, capabilities = created
    assert len(capabilities) == 3
    assert db.execute("SELECT count(*) FROM fulfilment_entitlements").fetchone()[0] == 3
    assert db.execute("SELECT state FROM fulfilment_notification_intents").fetchone()[0] == "prepared"
    assert db.execute("SELECT fulfilment_nonce FROM post_purchase_executions").fetchone()[0] == nonce
    stored = str(list(db.iterdump()))
    assert all(token not in stored for _, _, token, _ in capabilities)
    assert asyncio.run(prepare_notification(D1(db), "store", "test", "order", action, 101)) is None
    assert db.execute("SELECT count(*) FROM fulfilment_capabilities").fetchone()[0] == 3


def test_confirmed_purchase_dispatch_grants_and_cross_browser_access_without_duplicate_send():
    from urllib.parse import parse_qs, urlsplit

    from cartray.post_purchase import D1Dispatcher

    db = database(prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    sent = []
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    dispatcher = D1Dispatcher(
        D1(db),
        "store",
        "test",
        {"service": lambda _, e: sent.append(e.access_links) or "receipt"},
        pinned=True,
        fulfilment_origin="https://store.example.test",
    )
    assert asyncio.run(dispatcher.dispatch("order", (action,))) == {"workflow": "accepted"}
    assert len(sent) == 1 and len(sent[0]) == 3
    assert asyncio.run(dispatcher.dispatch("order", (action,))) == {"workflow": "already_claimed"}
    assert len(sent) == 1

    async def synthetic(resource, _config):
        return Stream(b"third-provider", resource.filename, resource.mime_type)

    registry = ProviderRegistry({"link": external_link, "object": private_object, "synthetic": synthetic})
    configs = {
        ("store", "0"): (CONFIGS[0], digest(CONFIGS[0])),
        ("store", "1"): ({"bucket": Bucket(), "key": "private/file"}, digest(CONFIGS[1])),
        ("store", "2"): ({}, digest(CONFIGS[2])),
    }
    tokens = [parse_qs(urlsplit(url).query)["token"][0] for _, url in sent[0]]

    def access(token):
        return asyncio.run(authorised_access(D1(db), token, registry, configs, 100))

    assert access(tokens[0]) == Redirect("https://example.test/p?id=1")
    assert access(tokens[1]) == Stream(b"dummy", "file.pdf", "application/pdf")
    assert access(tokens[2]) == Stream(b"third-provider", "file.pdf", "application/pdf")
    assert access(tokens[2]) == Stream(b"third-provider", "file.pdf", "application/pdf")


def test_ambiguous_delivery_is_held_and_never_retried():
    from cartray.post_purchase import D1Dispatcher

    db = database(prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    calls = []

    def timeout(_purchase, execution):
        calls.append(execution.access_links)
        raise TimeoutError("provider acceptance unknown")

    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    dispatcher = D1Dispatcher(
        D1(db), "store", "test", {"service": timeout}, pinned=True, fulfilment_origin="https://store.example.test"
    )
    assert asyncio.run(dispatcher.dispatch("order", (action,))) == {"workflow": "uncertain"}
    assert db.execute("SELECT state FROM fulfilment_notification_intents").fetchone()[0] == "sending"
    assert asyncio.run(dispatcher.dispatch("order", (action,))) == {"workflow": "already_claimed"}
    assert len(calls) == 1
    assert db.execute("SELECT count(*) FROM fulfilment_entitlements").fetchone()[0] == 3


def test_unconfirmed_purchase_never_claims_or_grants():
    db = database(prepopulate=False)
    db.execute("DELETE FROM outbox WHERE event_type='OrderConfirmed'")
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    assert asyncio.run(prepare_notification(D1(db), "store", "test", "order", action, 100)) is None
    assert db.execute("SELECT count(*) FROM post_purchase_executions").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM fulfilment_entitlements").fetchone()[0] == 0


def test_overlapping_workflows_keep_independent_grants_and_delivery_claims():
    from cartray.post_purchase import D1Dispatcher

    db = database(two_workflows=True, prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    calls = []
    actions = (
        Action("workflow", ("offer",), "service", "private/config", "a" * 64),
        Action("second", ("offer",), "service", "private/second", "b" * 64),
    )
    dispatcher = D1Dispatcher(
        D1(db),
        "store",
        "test",
        {"service": lambda _, e: calls.append(e) or "receipt"},
        pinned=True,
        fulfilment_origin="https://store.example.test",
    )
    assert asyncio.run(dispatcher.dispatch("order", actions)) == {"workflow": "accepted", "second": "accepted"}
    assert len(calls) == 2
    assert len(calls[0].access_links) == 3 and len(calls[1].access_links) == 1
    assert db.execute("SELECT count(*) FROM fulfilment_entitlements").fetchone()[0] == 4
    assert set(asyncio.run(dispatcher.dispatch("order", actions)).values()) == {"already_claimed"}
    assert len(calls) == 2


def test_stale_prepared_claim_can_be_safely_reissued_but_sending_cannot():
    from cartray.fulfilment import mark_notification_sending, recover_prepared_notification

    db = database(prepopulate=False)
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    first_nonce, first = asyncio.run(prepare_notification(D1(db), "store", "test", "order", action, 100))
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(
            recover_prepared_notification(
                D1(db), "store", "test", "order", "workflow", "operator", "confirmed no provider call", 1299
            )
        )
    nonce, replacement = asyncio.run(
        recover_prepared_notification(
            D1(db), "store", "test", "order", "workflow", "operator", "confirmed no provider call", 1300
        )
    )
    assert nonce != first_nonce
    assert {row[2] for row in first}.isdisjoint({row[2] for row in replacement})
    assert db.execute("SELECT count(*) FROM fulfilment_capabilities WHERE revoked_at IS NULL").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM fulfilment_recovery_audit").fetchone()[0] == 1
    from cartray.post_purchase import D1Dispatcher

    stale = D1Dispatcher(D1(db), "store", "test", {}, pinned=True, fulfilment_origin="https://store.example.test")
    assert asyncio.run(stale._mark_uncertain("order", "workflow", first_nonce)) is False
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "claimed"
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(mark_notification_sending(D1(db), "store", "test", "order", "workflow", first_nonce))
    asyncio.run(mark_notification_sending(D1(db), "store", "test", "order", "workflow", nonce))
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(
            recover_prepared_notification(
                D1(db), "store", "test", "order", "workflow", "operator", "provider outcome uncertain", 2600
            )
        )


def test_disabled_fulfilment_never_falls_back_to_legacy_delivery():
    from cartray.post_purchase import D1Dispatcher

    db = database()
    calls = []
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    dispatcher = D1Dispatcher(D1(db), "store", "test", {"service": lambda *_: calls.append(True) or "receipt"})
    with pytest.raises(ValueError, match="pinned delivery mode"):
        asyncio.run(dispatcher.dispatch("order", (action,)))
    assert calls == []


def test_operator_recovery_sends_only_after_prepared_claim_is_reissued():
    from cartray.post_purchase import D1Dispatcher

    db = database(prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    _, old = asyncio.run(prepare_notification(D1(db), "store", "test", "order", action, 100))
    sent = []
    dispatcher = D1Dispatcher(
        D1(db),
        "store",
        "test",
        {"service": lambda p, e: sent.append(e) or "receipt"},
        pinned=True,
        fulfilment_origin="https://store.example.test",
    )
    assert (
        asyncio.run(dispatcher.recover_prepared("order", "workflow", "operator", "no provider call", now=1300))
        == "accepted"
    )
    assert len(sent) == 1 and len(sent[0].access_links) == 3
    assert all(token not in str(sent[0].access_links) for _, _, token, _ in old)
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "accepted"
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(dispatcher.recover_prepared("order", "workflow", "operator", "unknown", now=2600))
    assert len(sent) == 1


def test_pre_send_fence_failure_keeps_prepared_claim_recoverable(monkeypatch):
    import cartray.fulfilment as fulfilment
    from cartray.post_purchase import D1Dispatcher

    db = database(prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    calls = []

    async def failed_fence(*_args):
        raise RuntimeError("temporary D1 failure")

    monkeypatch.setattr(fulfilment, "mark_notification_sending", failed_fence)
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    dispatcher = D1Dispatcher(
        D1(db),
        "store",
        "test",
        {"service": lambda *_: calls.append(1) or "receipt"},
        pinned=True,
        fulfilment_origin="https://store.example.test",
    )
    assert asyncio.run(dispatcher.dispatch("order", (action,))) == {"workflow": "pending_claim"}
    assert calls == []
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "claimed"
    assert db.execute("SELECT state FROM fulfilment_notification_intents").fetchone()[0] == "prepared"


def test_deterministic_preparation_failure_is_held_without_blocking_other_workflows(monkeypatch):
    import cartray.fulfilment as fulfilment
    from cartray.post_purchase import D1Dispatcher

    db = database(two_workflows=True, prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    original = fulfilment.prepare_notification

    async def prepare(database, merchant, environment, order_id, action, now):
        if action.workflow_key == "workflow":
            raise ValueError("resource invalid")
        return await original(database, merchant, environment, order_id, action, now)

    monkeypatch.setattr(fulfilment, "prepare_notification", prepare)
    calls = []
    actions = (
        Action("workflow", ("offer",), "service", "private/config", "a" * 64),
        Action("second", ("offer",), "service", "private/second", "b" * 64),
    )
    dispatcher = D1Dispatcher(
        D1(db),
        "store",
        "test",
        {"service": lambda _, e: calls.append(e.workflow_key) or "ok"},
        pinned=True,
        fulfilment_origin="https://store.example.test",
    )
    assert asyncio.run(dispatcher.dispatch("order", actions)) == {"workflow": "uncertain", "second": "accepted"}
    assert calls == ["second"]
    assert (
        db.execute("SELECT failure_code FROM fulfilment_preparation_failures").fetchone()[0]
        == "resource_preparation_failed"
    )
    assert set(asyncio.run(dispatcher.dispatch("order", actions)).values()) == {"already_claimed"}


def test_expired_resource_and_wrong_merchant_are_denied_without_affecting_other_access():
    db = database(prepopulate=False)
    db.execute(
        "INSERT INTO fulfilment_entitlements VALUES (?,?,?,?,?,?,?,?,?)",
        ("store", "test", "order", "workflow", "offer", 0, 1, None, 11),
    )
    db.execute(
        "INSERT INTO fulfilment_capabilities VALUES (?,?,?,?,?,?,?,?,?)",
        (token_digest("0" * 43), "store", "test", "order", "workflow", "offer", 0, 1, None),
    )
    registry = ProviderRegistry({"link": external_link})
    settings = {("wrong-store", "0"): (CONFIGS[0], digest(CONFIGS[0]))}
    assert isinstance(asyncio.run(authorised_access(D1(db), "0" * 43, registry, settings, 10)), Unavailable)
    settings = {("store", "0"): (CONFIGS[0], digest(CONFIGS[0]))}
    assert asyncio.run(authorised_access(D1(db), "0" * 43, registry, settings, 10)) == Redirect(CONFIGS[0]["url"])
    assert isinstance(asyncio.run(authorised_access(D1(db), "0" * 43, registry, settings, 12)), Unavailable)


def test_provider_outage_does_not_revoke_another_resource():
    db = database()

    async def unavailable(*_args):
        raise TimeoutError("synthetic provider outage")

    registry = ProviderRegistry({"link": external_link, "synthetic": unavailable})
    settings = {("store", "0"): (CONFIGS[0], digest(CONFIGS[0])), ("store", "2"): ({}, digest(CONFIGS[2]))}
    assert isinstance(asyncio.run(authorised_access(D1(db), "2" * 43, registry, settings, 10)), Unavailable)
    assert asyncio.run(authorised_access(D1(db), "0" * 43, registry, settings, 10)) == Redirect(CONFIGS[0]["url"])
