"""Publication selection must notice the first activation of another source."""

import asyncio
import importlib.util
import sys
import types
from unittest.mock import patch
from urllib.parse import urlencode

import test_native_lifecycle as nl
from test_native_lifecycle import MODULE, Database, TestResponse

lifecycle = nl.lifecycle


def test_public_read_budget_rejects_before_database(lifecycle, monkeypatch):
    workers = types.ModuleType("workers")
    workers.Response = TestResponse
    workers.WorkerEntrypoint = object
    spec = importlib.util.spec_from_file_location("tested_reviews_budget", MODULE / "src/worker.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": workers, "native_lifecycle": lifecycle}):
        spec.loader.exec_module(module)

    keys = []

    class Limiter:
        allowed = False

        async def limit(self, options):
            keys.append(options["key"])
            return types.SimpleNamespace(success=self.allowed)

    async def wait(value, _timeout):
        return await value

    monkeypatch.setattr(module, "to_js", lambda value, **_: value)
    monkeypatch.setattr(module, "Object", types.SimpleNamespace(fromEntries=None))
    monkeypatch.setattr(module, "await_promise", wait)
    worker = module.Default()
    worker.env = types.SimpleNamespace(REVIEWS_MODE="read_only", REVIEWS_STORE_KEY="s", REVIEWS_ENVIRONMENT="test",
                                       REVIEWS_PUBLIC_INGRESS=Limiter(),
                                       REVIEWS_INSTALLATION_ID="installation-one", REVIEWS_DB=None)
    request = types.SimpleNamespace(method="GET", url="https://example.invalid/reviews/v1/subjects/p/summary",
                                    headers={"CF-Connecting-IP": "192.0.2.8"})
    for path in ("/reviews/v1/subjects/p/summary", "/reviews/v1/subjects/p/contributions",
                 "/reviews/v1/subject-lookup?alias=%2Fp%2F"):
        request.url = "https://example.invalid" + path
        assert asyncio.run(worker.fetch(request)).status == 429
    assert len(keys) == 3
    request.headers = {}
    assert asyncio.run(worker.fetch(request)).status == 503
    assert len(keys) == 3
    request.headers = {"CF-Connecting-IP": "192.0.2.8"}
    for invalid in (None, "", "   ", 42):
        worker.env.REVIEWS_INSTALLATION_ID = invalid
        assert asyncio.run(worker.fetch(request)).status == 503
    assert len(keys) == 3
    worker.env.REVIEWS_INSTALLATION_ID = "installation-one"
    worker.env.REVIEWS_PUBLIC_INGRESS.allowed = "true"
    assert asyncio.run(worker.fetch(request)).status == 503
    worker.env.REVIEWS_PUBLIC_INGRESS.allowed = True
    worker.env.REVIEWS_DB = Database()
    request.url = "https://example.invalid/reviews/v1/subjects/p/summary"
    assert asyncio.run(worker.fetch(request)).status == 404
    worker.env.REVIEWS_INSTALLATION_ID = "installation-two"
    assert asyncio.run(worker.fetch(request)).status == 404
    assert keys[-1] != keys[-2]
    worker.env.REVIEWS_ENVIRONMENT = "live"
    worker.env.REVIEWS_PUBLIC_ADMISSION = "test_unlimited"
    assert asyncio.run(worker.fetch(request)).status == 503


def test_single_source_selection_invalidates_on_other_source_activation(lifecycle):
    workers = types.ModuleType("workers")
    workers.Response = TestResponse
    workers.WorkerEntrypoint = object
    spec = importlib.util.spec_from_file_location("tested_reviews_worker", MODULE / "src/worker.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": workers, "native_lifecycle": lifecycle}):
        spec.loader.exec_module(module)
    db = Database()
    db.db.execute("INSERT INTO review_snapshots VALUES ('s','test','one',1,?)", ("a" * 64,))
    db.db.execute("INSERT INTO review_snapshot_subjects VALUES ('s','test','one','p','product','/p/')")
    db.db.execute("INSERT INTO review_publications VALUES ('s','test','one',1)")
    args = (db, "s", "test", "p")
    historical = asyncio.run(module.subject_sources(*args))
    assert asyncio.run(module.publication_unchanged(*args, historical))
    db.db.execute("INSERT INTO review_subjects VALUES ('s','test','p','product','/p/')")
    db.db.execute("INSERT INTO native_publication_versions VALUES ('s','test','p',0,0,0,0)")
    assert not asyncio.run(module.publication_unchanged(*args, historical))
    db.db.execute("DELETE FROM review_publications")
    native = asyncio.run(module.subject_sources(*args))
    assert asyncio.run(module.publication_unchanged(*args, native))
    db.db.execute("INSERT INTO review_publications VALUES ('s','test','one',1)")
    assert not asyncio.run(module.publication_unchanged(*args, native))

    request = types.SimpleNamespace(method="GET", url="https://example.invalid/reviews/v1/subjects/p/summary")
    worker = module.Default()
    worker.env = types.SimpleNamespace(
        REVIEWS_MODE="read_only", REVIEWS_STORE_KEY="s", REVIEWS_ENVIRONMENT="test", REVIEWS_DB=db,
        REVIEWS_PUBLIC_ADMISSION="test_unlimited"
    )
    original_sources = module.subject_sources
    db.db.execute("DELETE FROM native_publication_versions")
    db.db.execute("DELETE FROM review_subjects")
    calls = 0

    async def activate_native(*source_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            db.db.execute("INSERT INTO review_subjects VALUES ('s','test','p','product','/p/')")
            db.db.execute("INSERT INTO native_publication_versions VALUES ('s','test','p',0,0,0,0)")
        return await original_sources(*source_args)

    module.subject_sources = activate_native
    assert asyncio.run(worker.fetch(request)).status == 409
    db.db.execute("DELETE FROM review_publications")
    calls = 0

    async def activate_history(*source_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            db.db.execute("INSERT INTO review_publications VALUES ('s','test','one',1)")
        return await original_sources(*source_args)

    module.subject_sources = activate_history
    assert asyncio.run(worker.fetch(request)).status == 409
    long_path = "/a" * 1023 + "/"
    query = urlencode({"alias": long_path})
    assert len(query) > 512
    request.url = "https://example.invalid/reviews/v1/subject-lookup?" + query
    assert asyncio.run(worker.fetch(request)).status == 404
    request.url = "https://example.invalid/reviews/v1/subject-lookup?" + "x" * 6501
    assert asyncio.run(worker.fetch(request)).status == 400


def test_runtime_identity_requires_authenticated_test_request_and_version(lifecycle):
    workers = types.ModuleType("workers")
    workers.Response = TestResponse
    workers.WorkerEntrypoint = object
    spec = importlib.util.spec_from_file_location("tested_reviews_identity", MODULE / "src/worker.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": workers, "native_lifecycle": lifecycle}):
        spec.loader.exec_module(module)
    worker = module.Default()
    version = "11111111-1111-4111-8111-111111111111"
    worker.env = types.SimpleNamespace(REVIEWS_ENVIRONMENT="test", REVIEWS_AUTOMATION_INTAKE="test",
        REVIEWS_AUTOMATION_ACTIVATION_TOKEN="a" * 32, CF_VERSION_METADATA=types.SimpleNamespace(id=version))
    request = types.SimpleNamespace(
        method="GET", url="https://internal/reviews-internal/v1/runtime-identity", headers={}
    )
    assert asyncio.run(worker.fetch(request)).status == 404
    request.headers = {"authorization": "Bearer " + "a" * 32}
    response = asyncio.run(worker.fetch(request))
    assert response.body == {"version_id": version}
    assert response.headers["Cache-Control"] == "no-store"
    worker.env.CF_VERSION_METADATA = None
    assert asyncio.run(worker.fetch(request)).status == 503
    worker.env.REVIEWS_ENVIRONMENT = "production"
    assert asyncio.run(worker.fetch(request)).status == 404
