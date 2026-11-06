"""Exercise actual lifecycle decisions and SQL against SQLite through a small D1 adapter."""

import asyncio
import importlib.util
import secrets
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE = Path(__file__).resolve().parents[1]


class TestResponse:
    __test__ = False

    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers

    @classmethod
    def json(cls, body, status=200, headers=None):
        return cls(body, status, headers)


class Statement:
    def __init__(self, db, sql):
        self.db, self.sql, self.values = db, sql, ()

    def bind(self, *values):
        self.values = values
        return self

    async def all(self):
        cursor = self.db.execute(self.sql, self.values)
        return types.SimpleNamespace(results=[dict(row) for row in cursor.fetchall()])


class Database:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        for migration in sorted((MODULE / "migrations").glob("*.sql")):
            self.db.executescript(migration.read_text())

    def prepare(self, sql):
        return Statement(self.db, sql)

    async def batch(self, statements):
        try:
            self.db.execute("BEGIN")
            results = []
            for statement in statements:
                cursor = self.db.execute(statement.sql, statement.values)
                results.append(types.SimpleNamespace(results=[dict(row) for row in cursor.fetchall()]))
            self.db.commit()
            return results
        except Exception:
            self.db.rollback()
            raise


@pytest.fixture
def lifecycle():
    js = types.ModuleType("js")
    for name in ("Promise", "Uint8Array", "clearTimeout", "crypto", "setTimeout"):
        setattr(js, name, None)
    js.Object = types.SimpleNamespace(fromEntries=None)
    ffi = types.ModuleType("pyodide.ffi")
    ffi.create_proxy = ffi.to_js = lambda value, **_kwargs: value
    pyodide = types.ModuleType("pyodide")
    pyodide.ffi = ffi
    workers = types.ModuleType("workers")
    workers.Response = TestResponse
    spec = importlib.util.spec_from_file_location("tested_native_lifecycle", MODULE / "src/native_lifecycle.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"js": js, "workers": workers, "pyodide": pyodide, "pyodide.ffi": ffi}):
        spec.loader.exec_module(module)
    module.random_token = lambda: secrets.token_hex(32)
    module.actual_read_json = module.read_json

    async def decode(request):
        return request.data

    module.read_json = decode
    return module


def invoke(module, env, path, data, *, admin=False, key=None):
    if not hasattr(env, "REVIEWS_INGRESS"):

        async def allow(_options):
            return types.SimpleNamespace(success=True)

        env.REVIEWS_INGRESS = types.SimpleNamespace(limit=allow)
    headers = {}
    if admin:
        headers["Authorization"] = "Bearer " + env.REVIEWS_TEST_ADMIN_TOKEN
    if key:
        headers["Idempotency-Key"] = key
    request = types.SimpleNamespace(method="POST", headers=headers, data=data, body=None)
    return asyncio.run(module.handle_write(request, env, path))


def test_native_lifecycle_with_real_transactions(lifecycle):
    db = Database()
    env = types.SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    subject = {"subject_id": "policy", "kind": "product", "path": "/policy/"}
    subjects = "/reviews-admin/v1/subjects"
    capabilities = "/reviews-admin/v1/test-capabilities"
    submissions = "/reviews/v1/contributions"
    assert invoke(lifecycle, env, subjects, subject).status == 401
    assert invoke(lifecycle, env, subjects, subject | {"path": "bad"}, admin=True).status == 400
    assert invoke(lifecycle, env, subjects, subject, admin=True).status == 201
    assert invoke(lifecycle, env, subjects, subject, admin=True).status == 409
    issued = invoke(lifecycle, env, capabilities, {"subject_id": "policy", "test_reviewer": "guest"}, admin=True)
    assert issued.status == 201
    data = {
        "subject_id": "policy",
        "author": "Guest",
        "body": "Hello",
        "rating": 2,
        "test_capability": issued.body["test_capability"],
    }
    assert invoke(lifecycle, env, submissions, data | {"rating": -1}, key="one").status == 400
    first = invoke(lifecycle, env, submissions, data, key="one")
    assert first.status == 202
    assert invoke(lifecycle, env, submissions, data, key="one").body == first.body
    assert invoke(lifecycle, env, submissions, data | {"body": "changed"}, key="one").status == 409
    rid, revision = first.body["review_id"], first.body["revision_id"]
    mod = "/reviews-admin/v1/contributions/" + rid + "/"
    assert invoke(lifecycle, env, mod + "approve", {"revision_id": revision}, admin=True).status == 200
    summary = asyncio.run(lifecycle.native_read(db, "store", "test", "policy", "summary", {}, 1))
    assert summary.body["rating"] == {"average": 2, "rated_count": 1}
    listing = asyncio.run(lifecycle.native_read(db, "store", "test", "policy", "contributions", {}, 1))
    assert listing.body["contributions"][0]["body"] == "Hello"
    assert "test_capability" not in str(listing.body)
    isolated = asyncio.run(lifecycle.native_read(db, "other-store", "test", "policy", "summary", {}, 1))
    assert isolated.status == 503
    assert (
        asyncio.run(lifecycle.native_read(db, "store", "test", "policy", "contributions", {"limit": ["0"]}, 1)).status
        == 400
    )
    assert (
        asyncio.run(
            lifecycle.native_read(db, "store", "test", "policy", "contributions", {"snapshot": ["native-1"]}, 1)
        ).status
        == 400
    )
    assert asyncio.run(lifecycle.native_read(db, "store", "test", "policy", "contributions", {}, 0)).status == 409
    assert invoke(lifecycle, env, mod + "reject", {"revision_id": revision}, admin=True).status == 409
    assert invoke(lifecycle, env, mod + "withdraw", {"revision_id": revision}, admin=True).status == 200
    summary = asyncio.run(lifecycle.native_read(db, "store", "test", "policy", "summary", {}, 2))
    assert summary.body["rating"] == {"average": None, "rated_count": 0}
    assert db.db.execute("SELECT COUNT(*) FROM native_audit").fetchone()[0] == 3


def test_invalid_submission_and_rejection_do_not_publish(lifecycle):
    db = Database()
    env = types.SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    subject = {"subject_id": "service", "kind": "service", "path": "/service/"}
    assert invoke(lifecycle, env, "/reviews-admin/v1/subjects", subject, admin=True).status == 201
    cap = "/reviews-admin/v1/test-capabilities"
    assert invoke(lifecycle, env, cap, {"subject_id": "service"}, admin=True).status == 400
    assert invoke(lifecycle, env, cap, {"subject_id": "missing", "test_reviewer": "x"}, admin=True).status == 429
    proof = invoke(lifecycle, env, cap, {"subject_id": "service", "test_reviewer": "x"}, admin=True).body[
        "test_capability"
    ]
    path = "/reviews/v1/contributions"
    base = {"subject_id": "service", "author": "User", "test_capability": proof}
    for invalid in (
        base,
        base | {"rating": True},
        base | {"rating": 6},
        base | {"rating": 2, "author": " "},
        base | {"rating": 2, "test_capability": "invalid"},
        base | {"rating": 2, "extra": "not permitted"},
    ):
        assert invoke(lifecycle, env, path, invalid, key="invalid").status == 400
    accepted = invoke(lifecycle, env, path, base | {"body": "Unrated"}, key="valid")
    assert accepted.status == 202
    mod = "/reviews-admin/v1/contributions/" + accepted.body["review_id"] + "/"
    revision = accepted.body["revision_id"]
    assert invoke(lifecycle, env, mod + "reject", {"revision_id": "invalid"}, admin=True).status == 400
    assert invoke(lifecycle, env, mod + "reject", {"revision_id": revision}, admin=True).status == 200
    assert invoke(lifecycle, env, mod + "approve", {"revision_id": revision}, admin=True).status == 409
    summary = asyncio.run(lifecycle.native_read(db, "store", "test", "service", "summary", {}, 0))
    assert summary.body["contribution_count"] == 0
    assert summary.body["rating"] == {"average": None, "rated_count": 0}
    assert db.db.execute("SELECT COUNT(*) FROM native_effect_outbox").fetchone()[0] == 1


def test_live_environment_fails_closed(lifecycle):
    env = types.SimpleNamespace(
        REVIEWS_DB=Database(), REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="live", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    result = invoke(lifecycle, env, "/reviews-admin/v1/subjects", {}, admin=True)
    assert result.status == 503
    assert env.REVIEWS_DB.db.execute("SELECT COUNT(*) FROM review_subjects").fetchone()[0] == 0


def test_worker_enforces_modes_and_scope(lifecycle):
    workers = types.ModuleType("workers")
    workers.Response = TestResponse
    workers.WorkerEntrypoint = object
    spec = importlib.util.spec_from_file_location("tested_reviews_worker", MODULE / "src/worker.py")
    worker = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": workers, "native_lifecycle": lifecycle}):
        spec.loader.exec_module(worker)
    instance = worker.Default()
    instance.env = types.SimpleNamespace(
        REVIEWS_DB=Database(),
        REVIEWS_MODE="disabled",
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_TEST_ADMIN_TOKEN="s" * 40,
    )

    def fetch(path, method="GET"):
        request = types.SimpleNamespace(method=method, url="https://example.test" + path, headers={}, body=None)
        return asyncio.run(instance.fetch(request))

    path = "/reviews/v1/subjects/policy/summary"
    assert fetch(path).status == 404
    assert fetch("/reviews-internal/v1/automation-intake", "POST").status == 404
    assert fetch("/reviews-internal/v1/automation-recipient-evidence", "POST").status == 404
    assert fetch("/reviews-internal/v1/automation-evidence-status", "POST").status == 404
    instance.env.REVIEWS_MODE = "read_only"
    assert fetch("/reviews-admin/v1/subjects", "POST").status == 405
    assert fetch("/reviews-internal/v1/automation-intake", "POST").status == 405
    assert fetch("/reviews-internal/v1/automation-recipient-evidence", "POST").status == 405
    assert fetch("/reviews-internal/v1/automation-evidence-status", "POST").status == 405
    assert fetch("/reviews-admin/v1/pending").status == 404
    assert fetch(path + "?limit=abc").status == 400
    assert fetch(path + "?unknown=x").status == 400
    assert fetch(path).status == 404
    instance.env.REVIEWS_MODE = "collect"
    instance.env.REVIEWS_ENVIRONMENT = "live"
    assert fetch(path).status == 503
    instance.env.REVIEWS_ENVIRONMENT = "test"
    assert (
        invoke(
            lifecycle,
            instance.env,
            "/reviews-admin/v1/subjects",
            {"subject_id": "policy", "kind": "product", "path": "/"},
            admin=True,
        ).status
        == 201
    )
    response = fetch(path)
    assert response.status == 200
    assert response.body["rating"] == {"average": None, "rated_count": 0}


def test_body_validation_and_single_reader_cleanup(lifecycle):
    class Bytes:
        def __init__(self, value):
            self.value = value
            self.length = len(value)

        def to_py(self):
            return self.value

    class Reader:
        def __init__(self, chunks):
            self.chunks = iter(chunks)
            self.cancelled = 0
            self.released = 0

        async def read(self):
            value = next(self.chunks, None)
            if value == b"timeout":
                raise TimeoutError("deadline")
            return types.SimpleNamespace(done=value is None, value=Bytes(value) if value is not None else None)

        async def cancel(self):
            self.cancelled += 1

        def releaseLock(self):
            self.released += 1

    class Body:
        def __init__(self, reader):
            self.reader, self.acquired = reader, 0

        def getReader(self):
            self.acquired += 1
            return self.reader

    async def await_promise(value, _timeout):
        return await value

    lifecycle.await_promise = await_promise
    for raw, expected in (
        (b'{"a":1}', {"a": 1}),
        (b'{"a":1,"a":2}', None),
        (b"[1]", None),
        (b"[" * 2000 + b"]" * 2000, None),
    ):
        reader = Reader([raw])
        body = Body(reader)
        request = types.SimpleNamespace(
            headers={"Content-Length": str(len(raw)), "Content-Type": "application/json"}, body=body
        )
        if expected is None:
            with pytest.raises(ValueError):
                asyncio.run(lifecycle.actual_read_json(request))
        else:
            assert asyncio.run(lifecycle.actual_read_json(request)) == expected
        assert body.acquired == 1 and reader.released == 1
    reader = Reader([b"timeout"])
    body = Body(reader)
    request = types.SimpleNamespace(headers={"Content-Length": "10", "Content-Type": "application/json"}, body=body)
    with pytest.raises(ValueError):
        asyncio.run(lifecycle.actual_read_json(request))
    assert body.acquired == 1 and reader.cancelled == 1 and reader.released == 1
    reader = Reader([b"abc"])
    body = Body(reader)
    request = types.SimpleNamespace(headers={"Content-Length": "3"}, body=body)
    asyncio.run(lifecycle.discard_body(request))
    assert body.acquired == 1 and reader.released == 1 and reader.cancelled == 0
    reader = Reader([])
    body = Body(reader)
    request = types.SimpleNamespace(headers={"Content-Length": "20000"}, body=body)
    asyncio.run(lifecycle.discard_body(request))
    assert body.acquired == 1 and reader.cancelled == 1 and reader.released == 1
    reader = Reader([b"timeout"])
    body = Body(reader)
    request = types.SimpleNamespace(headers={"Content-Length": "10"}, body=body)
    asyncio.run(lifecycle.discard_body(request))
    assert body.acquired == 1 and reader.cancelled == 1 and reader.released == 1


def test_moderation_queue_is_scoped_and_private(lifecycle):
    db = Database()
    env = types.SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    assert (
        invoke(
            lifecycle,
            env,
            "/reviews-admin/v1/subjects",
            {"subject_id": "policy", "kind": "product", "path": "/"},
            admin=True,
        ).status
        == 201
    )
    proof = invoke(
        lifecycle,
        env,
        "/reviews-admin/v1/test-capabilities",
        {"subject_id": "policy", "test_reviewer": "guest"},
        admin=True,
    ).body["test_capability"]
    assert (
        invoke(
            lifecycle,
            env,
            "/reviews/v1/contributions",
            {"subject_id": "policy", "author": "Guest", "body": "Private", "test_capability": proof},
            key="key",
        ).status
        == 202
    )
    request = types.SimpleNamespace(headers={})
    assert asyncio.run(lifecycle.pending_read(request, env, "")).status == 401
    request.headers["Authorization"] = "Bearer " + env.REVIEWS_TEST_ADMIN_TOKEN
    assert asyncio.run(lifecycle.pending_read(request, env, "bad=value")).status == 400
    assert asyncio.run(lifecycle.pending_read(request, env, "after=bad")).status == 400
    response = asyncio.run(lifecycle.pending_read(request, env, ""))
    assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.body["pending"][0]["body"] == "Private"
    assert proof not in str(response.body)
    env.REVIEWS_STORE_KEY = "another-store"
    assert asyncio.run(lifecycle.pending_read(request, env, "")).body["pending"] == []
    env.REVIEWS_ENVIRONMENT = "live"
    assert asyncio.run(lifecycle.pending_read(request, env, "")).status == 503


def test_public_pagination_and_rollback(lifecycle):
    db = Database()
    env = types.SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    assert (
        invoke(
            lifecycle,
            env,
            "/reviews-admin/v1/subjects",
            {"subject_id": "policy", "kind": "product", "path": "/"},
            admin=True,
        ).status
        == 201
    )
    reviews = []
    for guest in ("one", "two"):
        proof = invoke(
            lifecycle,
            env,
            "/reviews-admin/v1/test-capabilities",
            {"subject_id": "policy", "test_reviewer": guest},
            admin=True,
        ).body["test_capability"]
        result = invoke(
            lifecycle,
            env,
            "/reviews/v1/contributions",
            {"subject_id": "policy", "author": guest, "body": guest, "test_capability": proof},
            key=guest,
        )
        assert result.status == 202
        reviews.append(result.body)
        action = "/reviews-admin/v1/contributions/" + result.body["review_id"] + "/approve"
        assert invoke(lifecycle, env, action, {"revision_id": result.body["revision_id"]}, admin=True).status == 200

    def read(params):
        return asyncio.run(lifecycle.native_read(db, "store", "test", "policy", "contributions", params, 2))

    page = read({"limit": ["1"]})
    assert page.body["has_more"] and len(page.body["contributions"]) == 1
    cursor = page.body["next_after"]
    page2 = read({"limit": ["1"], "after": [cursor], "snapshot": ["native-2"]})
    assert len(page2.body["contributions"]) == 1 and not page2.body["has_more"]
    assert page.body["contributions"][0]["id"] != page2.body["contributions"][0]["id"]
    assert read({"after": ["f" * 64], "snapshot": ["native-2"]}).status == 400
    assert read({"after": [cursor], "snapshot": ["native-1"]}).status == 400
    assert read({"limit": ["bad"]}).status == 400
    before = db.db.execute("SELECT COUNT(*) FROM native_audit").fetchone()[0]
    db.db.execute("DELETE FROM native_publication_versions WHERE subject_id='policy'")
    db.db.commit()
    action = "/reviews-admin/v1/contributions/" + reviews[0]["review_id"] + "/withdraw"
    assert invoke(lifecycle, env, action, {"revision_id": reviews[0]["revision_id"]}, admin=True).status == 409
    assert db.db.execute("SELECT COUNT(*) FROM native_audit").fetchone()[0] == before
    assert (
        db.db.execute("SELECT state FROM native_reviews WHERE review_id=?", (reviews[0]["review_id"],)).fetchone()[0]
        == "published"
    )


def test_automation_intake_authentication_and_fail_closed_configuration(lifecycle):
    from test_automation_intake import setup

    checkout, reviews = setup()
    token = "automation-synthetic-token-0123456789"
    env = types.SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=checkout,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="s" * 40,
        REVIEWS_INVITATION_KEY="i" * 40,
        REVIEWS_REVIEWER_KEY="r" * 40,
        REVIEWS_CONSENT_SECRET="c" * 40,
        REVIEWS_DELIVERY_TOKEN="d" * 40,
        REVIEWS_SUBJECT_MAPS='{"v1":{"product_1":"policy"}}',
    )
    path = "/reviews-internal/v1/automation-intake"

    def post(data, bearer=token):
        request = types.SimpleNamespace(
            method="POST", headers={"Authorization": "Bearer " + bearer}, data=data, body=None
        )
        return asyncio.run(lifecycle.handle_write(request, env, path))

    assert post({"outbox_id": 10}).status == 503
    env.REVIEWS_AUTOMATION_INTAKE = "test"
    env.REVIEWS_AUTOMATION_INGRESS_TOKEN = env.REVIEWS_SETTLEMENT_TOKEN
    assert post({"outbox_id": 10}).status == 503
    env.REVIEWS_AUTOMATION_INGRESS_TOKEN = token
    assert post({"outbox_id": 10}, bearer="wrong").status == 401
    assert post({"outbox_id": 10, "extra": True}).status == 400
    assert post({"outbox_id": 999}).status == 409
    with patch.object(lifecycle.time, "time", return_value=120):
        result = post({"outbox_id": 10})
        assert result.status == 200
        assert result.body == {"status": "accepted", "due_at": 110}
        assert post({"outbox_id": 10}).body == result.body
    assert reviews.db.execute("SELECT state FROM review_automation_intents").fetchone()[0] == (
        "held_eligibility_recipient_consent"
    )
    env.REVIEWS_ENVIRONMENT = "live"
    assert post({"outbox_id": 10}).status == 503
    assert checkout.db.execute("SELECT delivered_at FROM outbox WHERE id=10").fetchone()[0] is None


def test_automation_evidence_routes_require_independent_authorities(lifecycle):
    from test_automation_intake import run, setup
    from test_automation_recipient import RECIPIENT, RECIPIENT_SECRET, consent, signed

    checkout, reviews = setup()
    run(checkout, reviews)
    token = "automation-synthetic-token-0123456789"
    env = types.SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=checkout,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="s" * 40,
        REVIEWS_INVITATION_KEY="i" * 40,
        REVIEWS_REVIEWER_KEY="r" * 40,
        REVIEWS_CONSENT_SECRET="c" * 40,
        REVIEWS_DELIVERY_TOKEN="d" * 40,
        REVIEWS_AUTOMATION_INTAKE="test",
        REVIEWS_AUTOMATION_INGRESS_TOKEN=token,
    )
    recipient_path = "/reviews-internal/v1/automation-recipient-evidence"
    status_path = "/reviews-internal/v1/automation-evidence-status"
    data = {
        "outbox_id": 10,
        "recipient_context_hash": RECIPIENT,
        "binding_version": "v1",
        "evidence_reference": "synthetic-recipient",
        "verified_at": 100,
    }
    signature = signed("reviews-automation-recipient-v1", "store", "test", data, RECIPIENT_SECRET)

    def post(path, body, headers):
        request = types.SimpleNamespace(method="POST", headers=headers, data=body, body=None)
        return asyncio.run(lifecycle.handle_write(request, env, path))

    assert post(recipient_path, data, {"X-Reviews-Recipient-Signature": signature}).status == 503
    env.REVIEWS_RECIPIENT_EVIDENCE_SECRET = token
    assert post(recipient_path, data, {"X-Reviews-Recipient-Signature": signature}).status == 503
    env.REVIEWS_RECIPIENT_EVIDENCE_SECRET = RECIPIENT_SECRET.decode()
    assert post(recipient_path, data, {"X-Reviews-Recipient-Signature": "0" * 64}).status == 409
    with patch.object(lifecycle.time, "time", return_value=120):
        assert post(recipient_path, data, {"X-Reviews-Recipient-Signature": signature}).status == 200
        assert post(recipient_path, data, {"X-Reviews-Recipient-Signature": signature}).status == 200
        assert post(status_path, {"outbox_id": 10}, {"Authorization": "Bearer wrong"}).status == 401
        assert post(status_path, {"outbox_id": 10, "extra": True}, {"Authorization": "Bearer " + token}).status == 400
        assert post(status_path, {"outbox_id": 999}, {"Authorization": "Bearer " + token}).status == 409
        assert (
            post(status_path, {"outbox_id": 10}, {"Authorization": "Bearer " + token}).body["consent_granted"] is False
        )
        consent(checkout, reviews)
        result = post(status_path, {"outbox_id": 10}, {"Authorization": "Bearer " + token})
        assert result.status == 200 and result.body == {
            "state": "held_eligibility_recipient_consent",
            "recipient_binding_recorded": True,
            "consent_granted": True,
        }
        assert RECIPIENT not in str(result.body)
    env.REVIEWS_ENVIRONMENT = "live"
    assert post(recipient_path, data, {"X-Reviews-Recipient-Signature": signature}).status == 503
    assert reviews.db.execute("SELECT count(*) FROM invitation_generations").fetchone()[0] == 0


def test_due_evaluation_authentication_and_durable_receipts(lifecycle):
    from test_automation_intake import run, setup

    checkout, reviews = setup()
    run(checkout, reviews)
    token = "automation-synthetic-token-0123456789"
    env = types.SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=checkout,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="s" * 40,
        REVIEWS_INVITATION_KEY="i" * 40,
        REVIEWS_REVIEWER_KEY="r" * 40,
        REVIEWS_CONSENT_SECRET="c" * 40,
        REVIEWS_DELIVERY_TOKEN="d" * 40,
        REVIEWS_AUTOMATION_INGRESS_TOKEN=token,
    )
    path = "/reviews-internal/v1/automation-due-evaluate"
    body = {"acquisition_id": "order_1", "claim_token": "a" * 64}

    def post(data=body, bearer=token):
        request = types.SimpleNamespace(
            method="POST", headers={"Authorization": "Bearer " + bearer}, data=data, body=None
        )
        result = asyncio.run(lifecycle.handle_write(request, env, path))
        reviews.db.commit()
        return result

    assert post().status == 503
    env.REVIEWS_AUTOMATION_INTAKE = "test"
    env.REVIEWS_AUTOMATION_INGRESS_TOKEN = env.REVIEWS_CONSENT_SECRET
    assert post().status == 503
    env.REVIEWS_AUTOMATION_INGRESS_TOKEN = token
    assert post(bearer="wrong").status == 401
    assert post({"extra": True}).status == 400
    assert post({**body, "claim_token": "bad"}).status == 409
    with patch.object(lifecycle.time, "time", return_value=109):
        assert post().body == {"status": "unavailable"}
    with patch.object(lifecycle.time, "time", return_value=110):
        assert post().body == {"status": "held"}
        assert post().body == {"status": "held"}
        assert post({**body, "claim_token": "b" * 64}).body == {"status": "unavailable"}
    assert (
        reviews.db.execute("SELECT state FROM review_automation_intents").fetchone()[0]
        == "held_eligibility_recipient_consent"
    )
    assert reviews.db.execute("SELECT count(*) FROM invitation_generations").fetchone()[0] == 0
    env.REVIEWS_ENVIRONMENT = "live"
    assert post().status == 503
