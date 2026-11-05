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
            for statement in statements:
                self.db.execute(statement.sql, statement.values)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


@pytest.fixture
def lifecycle():
    js = types.ModuleType("js")
    for name in ("Promise", "Uint8Array", "clearTimeout", "crypto", "setTimeout"):
        setattr(js, name, None)
    ffi = types.ModuleType("pyodide.ffi")
    ffi.create_proxy = ffi.to_js = lambda value: value
    pyodide = types.ModuleType("pyodide")
    pyodide.ffi = ffi
    workers = types.ModuleType("workers")
    workers.Response = TestResponse
    spec = importlib.util.spec_from_file_location("tested_native_lifecycle", MODULE / "src/native_lifecycle.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"js": js, "workers": workers, "pyodide": pyodide, "pyodide.ffi": ffi}):
        spec.loader.exec_module(module)
    module.random_token = lambda: secrets.token_hex(32)

    async def decode(request):
        return request.data

    module.read_json = decode
    return module


def invoke(module, env, path, data, *, admin=False, key=None):
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
    instance.env.REVIEWS_MODE = "read_only"
    assert fetch("/reviews-admin/v1/subjects", "POST").status == 405
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
