"""Subject lookup is scoped, unambiguous and based on pinned aliases."""

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from cartray_reviews import lookup
from test_native_lifecycle import MODULE, Database, TestResponse
from test_unified import importer


def test_snapshot_alias_lookup_and_native_collision(monkeypatch):
    db = Database()

    def execute(_flag, filename):
        db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")

    monkeypatch.setattr(importer, "execute", execute)
    data = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
    data["schema"] = "cartray.reviews.snapshot.v4"
    data["source"]["public_attribution"] = "Example historical reviews"
    data["subjects"][0]["aliases"] = ["wp-270", "/old-product/"]
    for item in data["contributions"]:
        item["title"] = None
        item["date"]["source_local_raw"] = None
        item["date"]["source_utc_raw"] = None
    raw = json.dumps(data).encode()
    importer.import_snapshot(raw, digest=hashlib.sha256(raw).hexdigest(), store="example-store", environment="test")
    for sql, values, index in (
        (
            "SELECT * FROM review_snapshot_subjects WHERE store_key=? AND environment=? "
            "AND snapshot_id=? AND canonical_path=?",
            ("example-store", "test", "one", "/product/"),
            "review_snapshot_subject_path_idx",
        ),
        (
            "SELECT * FROM review_subjects WHERE store_key=? AND environment=? AND canonical_path=?",
            ("example-store", "test", "/product/"),
            "review_native_subject_path_idx",
        ),
    ):
        plan = db.db.execute("EXPLAIN QUERY PLAN " + sql, values).fetchall()
        assert any(index in row[3] for row in plan)

    def resolve(query, store="example-store"):
        return asyncio.run(lookup.resolve_subject(db, store, "test", query, TestResponse))

    expected = {"subject_id": "example-policy", "kind": "product", "canonical_path": "/product/"}
    for query in ("alias=wp-270", "alias=%2Fold-product%2F", "alias=example-policy", "alias=%2Fproduct%2F"):
        result = resolve(query)
        assert result.status == 200 and result.body == expected and result.headers["Cache-Control"] == "no-store"
    assert resolve("alias=wp-270", store="other-store").status == 404
    assert resolve("alias=wp-270&alias=other").status == 400
    changed = json.loads(json.dumps(data))
    changed["snapshot_id"], changed["publication_revision"] = "two", 2
    changed["subjects"][0]["aliases"] = ["/old-product/"]
    changed_bytes = json.dumps(changed).encode()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        importer.import_snapshot(
            changed_bytes, digest=hashlib.sha256(changed_bytes).hexdigest(), store="example-store", environment="test"
        )
    db.db.rollback()
    assert resolve("alias=wp-270").body == expected
    db.db.execute("INSERT INTO review_subjects VALUES ('example-store','test','wp-270','product','/other/')")
    assert resolve("alias=wp-270").status == 409
    db.db.execute("INSERT INTO review_subjects VALUES ('example-store','test','other','product','/product/')")
    assert resolve("alias=%2Fproduct%2F").status == 409


def test_alias_cannot_shadow_another_snapshot_canonical_path():
    from cartray_reviews.snapshot import SnapshotError, load_snapshot

    data = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
    data["schema"] = "cartray.reviews.snapshot.v4"
    data["source"]["public_attribution"] = "Example historical reviews"
    data["subjects"][0]["aliases"] = ["/other/"]
    data["subjects"].append({"id": "other", "kind": "product", "path": "/other/", "aliases": []})
    data["summaries"].append({"subject_id": "other", "count": 0, "sum": 0, "histogram": [0, 0, 0, 0, 0]})
    for item in data["contributions"]:
        item["title"] = None
        item["date"]["source_local_raw"] = None
        item["date"]["source_utc_raw"] = None
    raw = json.dumps(data).encode()
    with pytest.raises(SnapshotError, match="canonical path"):
        load_snapshot(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), store="example-store", environment="test")


def test_lookup_fails_closed_if_publication_changes(monkeypatch):
    calls = 0

    async def changed(*_args):
        nonlocal calls
        calls += 1
        return [("p", "product", "/one/")] if calls == 1 else [("p", "product", "/two/")]

    monkeypatch.setattr(lookup, "_matches", changed)
    result = asyncio.run(lookup.resolve_subject(None, "s", "test", "alias=old", TestResponse))
    assert result.status == 409


def test_old_canonical_path_cannot_be_reassigned_or_lost(monkeypatch):
    db = Database()

    def execute(_flag, filename):
        try:
            db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")
        except Exception:
            db.db.rollback()
            raise

    monkeypatch.setattr(importer, "execute", execute)
    data = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
    data["schema"] = "cartray.reviews.snapshot.v4"
    data["source"]["public_attribution"] = "Example historical reviews"
    data["subjects"][0]["aliases"] = ["wp-270"]
    for item in data["contributions"]:
        item["title"] = None
        item["date"]["source_local_raw"] = None
        item["date"]["source_utc_raw"] = None
    raw = json.dumps(data).encode()
    importer.import_snapshot(raw, digest=hashlib.sha256(raw).hexdigest(), store="example-store", environment="test")
    moved = json.loads(json.dumps(data))
    moved["snapshot_id"], moved["publication_revision"] = "two", 2
    moved["subjects"][0]["path"] = "/new/"
    moved["subjects"].append({"id": "other", "kind": "product", "path": "/product/", "aliases": []})
    moved["summaries"].append({"subject_id": "other", "count": 0, "sum": 0, "histogram": [0, 0, 0, 0, 0]})
    changed = json.dumps(moved).encode()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        importer.import_snapshot(
            changed, digest=hashlib.sha256(changed).hexdigest(), store="example-store", environment="test"
        )
    assert db.db.execute("SELECT snapshot_id FROM review_publications").fetchone()[0] == "one"
    moved["subjects"].pop()
    moved["summaries"].pop()
    moved["subjects"][0]["aliases"].append("/product/")
    allowed = json.dumps(moved).encode()
    importer.import_snapshot(
        allowed, digest=hashlib.sha256(allowed).hexdigest(), store="example-store", environment="test"
    )
    result = asyncio.run(lookup.resolve_subject(db, "example-store", "test", "alias=%2Fproduct%2F", TestResponse))
    assert result.status == 200 and result.body["canonical_path"] == "/new/"
