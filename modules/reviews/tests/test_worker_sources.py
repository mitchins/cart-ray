"""Publication selection must notice the first activation of another source."""

import asyncio
import importlib.util
import sys
import types
from unittest.mock import patch

import test_native_lifecycle as nl
from test_native_lifecycle import MODULE, Database, TestResponse

lifecycle = nl.lifecycle


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
