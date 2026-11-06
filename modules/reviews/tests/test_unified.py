"""Exercise the combined projection and import with actual SQLite transactions."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_native_lifecycle as nl
from cartray_reviews.unified import unified_read
from test_native_lifecycle import Database, TestResponse, invoke

lifecycle = nl.lifecycle

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "tools"))
import import_snapshot as importer  # noqa: E402


def test_import_and_native_publication_share_subject(lifecycle, monkeypatch):
    db = Database()
    env = SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="example-store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    subject = {"subject_id": "example-policy", "kind": "product", "path": "/product/"}
    assert invoke(lifecycle, env, "/reviews-admin/v1/subjects", subject, admin=True).status == 201

    def execute(_flag, filename):
        db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")

    monkeypatch.setattr(importer, "execute", execute)
    fixture = (MODULE / "tests/fixtures/approved-synthetic.json").read_bytes()
    args = {"digest": hashlib.sha256(fixture).hexdigest(), "store": "example-store", "environment": "test"}
    importer.import_snapshot(fixture, **args)
    importer.import_snapshot(fixture, **args)
    assert db.db.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 1
    token = invoke(
        lifecycle,
        env,
        "/reviews-admin/v1/test-capabilities",
        {"subject_id": "example-policy", "test_reviewer": "guest"},
        admin=True,
    ).body["test_capability"]
    submitted = invoke(
        lifecycle,
        env,
        "/reviews/v1/contributions",
        {
            "subject_id": "example-policy",
            "author": "Guest",
            "body": "A private canary is absent",
            "rating": 2,
            "test_capability": token,
        },
        key="request-1",
    ).body
    path = "/reviews-admin/v1/contributions/" + submitted["review_id"] + "/"
    assert invoke(lifecycle, env, path + "approve", {"revision_id": submitted["revision_id"]}, admin=True).status == 200

    def read(endpoint, params=None):
        return asyncio.run(
            unified_read(db, "example-store", "test", "example-policy", endpoint, params or {}, TestResponse)
        )

    summary = read("summary").body
    assert summary["contribution_count"] == 4
    assert summary["rating"] == {"rated_count": 3, "average": 11 / 3}
    page = read("contributions", {"limit": ["1"]}).body
    seen = []
    while True:
        seen.extend(page["contributions"])
        if not page["has_more"]:
            break
        page = read(
            "contributions", {"limit": ["1"], "snapshot": [page["snapshot_id"]], "after": [page["next_after"]]}
        ).body
    assert len(seen) == 4
    historic = next(item for item in seen if item["id"] == "h-r")
    assert historic["replies"][0]["source_id"] == "a"
    assert historic["verification"] == "email_only"
    assert "test_capability" not in json.dumps(seen)
    assert (
        invoke(lifecycle, env, path + "withdraw", {"revision_id": submitted["revision_id"]}, admin=True).status == 200
    )
    assert read("summary").body["rating"] == {"rated_count": 2, "average": 4.5}
    assert read("contributions", {"snapshot": [summary["snapshot_id"]], "after": ["h-r"]}).status == 400


def test_mismatched_subject_import_rolls_back(monkeypatch):
    db = Database()
    db.db.execute("INSERT INTO review_subjects VALUES ('example-store','test','example-policy','service','/other/')")
    db.db.commit()

    def execute(_flag, filename):
        try:
            db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")
        except Exception:
            db.db.rollback()
            raise

    monkeypatch.setattr(importer, "execute", execute)
    fixture = (MODULE / "tests/fixtures/approved-synthetic.json").read_bytes()
    with pytest.raises(Exception):
        importer.import_snapshot(
            fixture, digest=hashlib.sha256(fixture).hexdigest(), store="example-store", environment="test"
        )
    assert db.db.execute("SELECT COUNT(*) FROM review_publications").fetchone()[0] == 0


def test_version_change_during_projection_fails_closed(monkeypatch):
    import cartray_reviews.unified as projection

    db = Database()
    db.db.execute("INSERT INTO review_subjects VALUES ('s','test','p','product','/p/')")
    db.db.execute("INSERT INTO native_publication_versions VALUES ('s','test','p',0,0,0,0)")
    db.db.execute("INSERT INTO review_snapshots VALUES ('s','test','one',1,?)", ("a" * 64,))
    db.db.execute("INSERT INTO review_snapshot_subjects VALUES ('s','test','one','p','product','/p/')")
    db.db.execute("INSERT INTO review_publications VALUES ('s','test','one',1)")
    real_versions = projection._versions
    calls = 0

    async def versions(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            db.db.execute("UPDATE native_publication_versions SET version=version+1")
        return await real_versions(*args)

    monkeypatch.setattr(projection, "_versions", versions)
    result = asyncio.run(unified_read(db, "s", "test", "p", "summary", {}, TestResponse))
    assert result.status == 409


def test_v2_import_preserves_title_and_source_provenance(monkeypatch):
    db = Database()

    def execute(_flag, filename):
        db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")

    monkeypatch.setattr(importer, "execute", execute)
    data = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
    data["schema"] = "cartray.reviews.snapshot.v2"
    for item in data["contributions"]:
        item["title"] = "Historical title" if item["id"] == "r" else None
        item["date"]["source_local_raw"] = "2024-01-02 11:00:00"
        item["date"]["source_utc_raw"] = None
    content = json.dumps(data).encode()
    importer.import_snapshot(
        content, digest=hashlib.sha256(content).hexdigest(), store="example-store", environment="test"
    )
    provenance = db.db.execute(
        "SELECT source_identity, extracted_at, source_sha256 FROM review_snapshot_provenance"
    ).fetchone()
    assert tuple(provenance) == (data["source"]["identity"], data["source"]["extracted_at"], data["source"]["sha256"])
    title = db.db.execute("SELECT title FROM review_import_titles WHERE contribution_id='r'").fetchone()
    assert title["title"] == "Historical title"
    date = json.loads(
        db.db.execute("SELECT source_date FROM review_contributions WHERE contribution_id='r'").fetchone()[0]
    )
    assert date["source_local_raw"] == "2024-01-02 11:00:00"
