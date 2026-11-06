"""Exercise the combined projection and import with actual SQLite transactions."""

import asyncio
import hashlib
import json
import sqlite3
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
import reconcile_identities as reconciliation  # noqa: E402


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
    staff_reply = invoke(
        lifecycle,
        env,
        path + "reply",
        {
            "parent_revision_id": submitted["revision_id"],
            "expected_reply_revision_id": None,
            "author": "Store",
            "body": "Thanks for your review",
        },
        admin=True,
        key="staff-reply",
    )
    assert staff_reply.status == 200

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
    native = next(item for item in seen if item["id"] == "n-" + submitted["review_id"])
    assert native["replies"][0]["id"] == "n-" + staff_reply.body["reply_id"]
    assert native["replies"][0]["body"] == "Thanks for your review"
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
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
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


def test_source_id_collision_and_same_day_pagination(monkeypatch):
    db = Database()

    def execute(_flag, filename):
        db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")

    monkeypatch.setattr(importer, "execute", execute)
    fixture = (MODULE / "tests/fixtures/approved-synthetic.json").read_bytes()
    importer.import_snapshot(
        fixture, digest=hashlib.sha256(fixture).hexdigest(), store="example-store", environment="test"
    )
    db.db.execute("INSERT INTO review_subjects VALUES ('example-store','test','example-policy','product','/product/')")
    db.db.execute("INSERT INTO native_publication_versions VALUES ('example-store','test','example-policy',1,1,1,3)")
    db.db.execute(
        "INSERT INTO native_reviews VALUES ('example-store','test','r','example-policy','private',"
        "'published','revision-r',1704153600)"
    )
    db.db.execute(
        "INSERT INTO native_revisions VALUES ('example-store','test','revision-r','r','Native',NULL,'text',3)"
    )

    def read(params):
        return asyncio.run(
            unified_read(db, "example-store", "test", "example-policy", "contributions", params, TestResponse)
        )

    params, found = {"limit": ["1"]}, []
    while True:
        page = read(params).body
        found.extend(page["contributions"])
        if not page["has_more"]:
            break
        params = {"limit": ["1"], "after": [page["next_after"]], "snapshot": [page["snapshot_id"]]}
    assert [item["id"] for item in found] == ["h-u", "h-r", "h-r2", "n-r"]
    assert found[1]["replies"][0]["id"] == "h-a"


def test_snapshot_promotion_during_read_fails_closed(monkeypatch):
    import cartray_reviews.unified as projection

    db = Database()
    db.db.execute("INSERT INTO review_subjects VALUES ('s','test','p','product','/p/')")
    db.db.execute("INSERT INTO native_publication_versions VALUES ('s','test','p',0,0,0,0)")
    db.db.execute("INSERT INTO review_snapshots VALUES ('s','test','one',1,?)", ("a" * 64,))
    db.db.execute("INSERT INTO review_snapshots VALUES ('s','test','two',2,?)", ("b" * 64,))
    db.db.execute("INSERT INTO review_snapshot_subjects VALUES ('s','test','one','p','product','/p/')")
    db.db.execute("INSERT INTO review_snapshot_subjects VALUES ('s','test','two','p','product','/p/')")
    db.db.execute("INSERT INTO review_publications VALUES ('s','test','one',1)")
    original = projection._versions
    calls = 0

    async def promote(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            db.db.execute("UPDATE review_publications SET snapshot_id='two',revision=2")
        return await original(*args)

    monkeypatch.setattr(projection, "_versions", promote)
    result = asyncio.run(unified_read(db, "s", "test", "p", "summary", {}, TestResponse))
    assert result.status == 409


def test_response_size_trimming_preserves_cursor_continuity():
    db = Database()
    db.db.execute("INSERT INTO review_subjects VALUES ('s','test','p','product','/p/')")
    db.db.execute("INSERT INTO native_publication_versions VALUES ('s','test','p',1,20,20,60)")
    db.db.execute("INSERT INTO review_snapshots VALUES ('s','test','one',1,?)", ("a" * 64,))
    db.db.execute("INSERT INTO review_snapshot_subjects VALUES ('s','test','one','p','product','/p/')")
    db.db.execute("INSERT INTO review_publications VALUES ('s','test','one',1)")
    for i in range(20):
        rid = f"{i:064x}"
        db.db.execute(
            "INSERT INTO native_reviews VALUES ('s','test',?,'p',?,'published',?,1704153600)",
            (rid, "private-" + rid, "revision-" + rid),
        )
        db.db.execute(
            "INSERT INTO native_revisions VALUES ('s','test',?,?, 'author',NULL,?,3)",
            ("revision-" + rid, rid, "x" * 12000),
        )
    params, found = {"limit": ["50"]}, []
    while True:
        page = asyncio.run(unified_read(db, "s", "test", "p", "contributions", params, TestResponse)).body
        assert len(json.dumps(page, ensure_ascii=True).encode("ascii")) <= 128000
        found.extend(row["source_id"] for row in page["contributions"])
        if not page["has_more"]:
            break
        assert page["next_after"] == page["contributions"][-1]["id"]
        params = {"limit": ["50"], "after": [page["next_after"]], "snapshot": [page["snapshot_id"]]}
    assert found == [f"{i:064x}" for i in range(20)]


def test_adjudicated_identity_link_preserves_reply_and_withdrawal(lifecycle, monkeypatch):
    db = Database()
    env = SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="example-store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    assert (
        invoke(
            lifecycle,
            env,
            "/reviews-admin/v1/subjects",
            {"subject_id": "example-policy", "kind": "product", "path": "/product/"},
            admin=True,
        ).status
        == 201
    )

    def execute(_flag, filename):
        try:
            db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")
        except Exception:
            db.db.rollback()
            raise

    monkeypatch.setattr(importer, "execute", execute)
    fixture = (MODULE / "tests/fixtures/approved-synthetic.json").read_bytes()
    importer.import_snapshot(
        fixture, digest=hashlib.sha256(fixture).hexdigest(), store="example-store", environment="test"
    )
    token = invoke(
        lifecycle,
        env,
        "/reviews-admin/v1/test-capabilities",
        {"subject_id": "example-policy", "test_reviewer": "guest"},
        admin=True,
    ).body["test_capability"]
    review = invoke(
        lifecycle,
        env,
        "/reviews/v1/contributions",
        {"subject_id": "example-policy", "author": "Guest", "body": "Updated", "rating": 2, "test_capability": token},
        key="identity-request",
    ).body
    path = "/reviews-admin/v1/contributions/" + review["review_id"] + "/"
    assert invoke(lifecycle, env, path + "approve", {"revision_id": review["revision_id"]}, admin=True).status == 200

    def read(endpoint, params=None):
        return asyncio.run(
            unified_read(db, "example-store", "test", "example-policy", endpoint, params or {}, TestResponse)
        )

    before = read("summary").body["snapshot_id"]
    monkeypatch.setattr(reconciliation, "execute", execute)
    manifest = json.dumps(
        {
            "schema": "cartray.reviews.identity-links.v1",
            "store": "example-store",
            "environment": "test",
            "links": [
                {
                    "subject_id": "example-policy",
                    "source_identity": "synthetic-native",
                    "source_contribution_id": "r",
                    "source_revision_id": "r1",
                    "source_snapshot_sha256": hashlib.sha256(fixture).hexdigest(),
                    "native_review_id": review["review_id"],
                    "native_revision_id": review["revision_id"],
                    "evidence_sha256": "a" * 64,
                }
            ],
        }
    ).encode()
    manifest_args = {"sha256": hashlib.sha256(manifest).hexdigest(), "store": "example-store", "environment": "test"}
    unreviewed = json.loads(manifest)
    unreviewed["links"][0]["source_snapshot_sha256"] = "b" * 64
    unreviewed_bytes = json.dumps(unreviewed).encode()
    with pytest.raises(sqlite3.IntegrityError, match="invalid identity link"):
        reconciliation.apply_manifest(
            unreviewed_bytes, **(manifest_args | {"sha256": hashlib.sha256(unreviewed_bytes).hexdigest()})
        )
    assert db.db.execute("SELECT COUNT(*) FROM review_identity_links").fetchone()[0] == 0
    reconciliation.apply_manifest(manifest, **manifest_args)
    reconciliation.apply_manifest(manifest, **manifest_args)
    with pytest.raises(ValueError, match="digest"):
        reconciliation.apply_manifest(manifest, **(manifest_args | {"sha256": "b" * 64}))
    conflict = manifest.replace(b'"aaaaaaaa', b'"baaaaaaa', 1)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        reconciliation.apply_manifest(conflict, **(manifest_args | {"sha256": hashlib.sha256(conflict).hexdigest()}))
    assert db.db.execute("SELECT COUNT(*) FROM review_identity_links").fetchone()[0] == 1
    after = read("summary").body
    assert before != after["snapshot_id"]
    assert after["contribution_count"] == 3
    assert after["rating"] == {"rated_count": 2, "average": 3}
    listing = read("contributions").body["contributions"]
    assert all(item["id"] != "h-r" for item in listing)
    linked = next(item for item in listing if item["id"] == "n-" + review["review_id"])
    assert linked["historical_provenance"]["anchors"] == ["review-r"]
    assert linked["historical_provenance"]["verification"] == "email_only"
    assert linked["replies"][0]["id"] == "h-a"
    changed = json.loads(fixture)
    changed["snapshot_id"], changed["publication_revision"] = "two", 2
    changed["contributions"][0]["anchors"] = ["changed-anchor"]
    changed_bytes = json.dumps(changed).encode()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        importer.import_snapshot(
            changed_bytes, digest=hashlib.sha256(changed_bytes).hexdigest(), store="example-store", environment="test"
        )
    assert db.db.execute("SELECT snapshot_id FROM review_publications").fetchone()[0] == "one"
    changed = json.loads(fixture)
    changed["snapshot_id"], changed["publication_revision"] = "two", 2
    changed["contributions"] = [item for item in changed["contributions"] if item["id"] != "a"]
    changed_bytes = json.dumps(changed).encode()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        importer.import_snapshot(
            changed_bytes, digest=hashlib.sha256(changed_bytes).hexdigest(), store="example-store", environment="test"
        )
    assert db.db.execute("SELECT snapshot_id FROM review_publications").fetchone()[0] == "one"
    assert invoke(lifecycle, env, path + "withdraw", {"revision_id": review["revision_id"]}, admin=True).status == 200
    assert read("summary").body["contribution_count"] == 2
    public = json.dumps(read("contributions").body)
    assert "Updated" not in public and '"id": "h-r"' not in public and "reply-a" not in public
    removed = json.loads(fixture)
    removed["snapshot_id"], removed["publication_revision"] = "removed", 2
    removed["contributions"] = [item for item in removed["contributions"] if item["id"] not in ("r", "a")]
    removed["summaries"][0].update({"count": 1, "sum": 4, "histogram": [0, 0, 0, 1, 0]})
    removed_bytes = json.dumps(removed).encode()
    importer.import_snapshot(
        removed_bytes, digest=hashlib.sha256(removed_bytes).hexdigest(), store="example-store", environment="test"
    )
    assert read("summary").body["rating"] == {"rated_count": 1, "average": 4}
    assert '"id": "h-r"' not in json.dumps(read("contributions").body)
    for parent, kind in (("u", "reply"), (None, "review")):
        reappearing = json.loads(json.dumps(removed))
        reappearing["snapshot_id"], reappearing["publication_revision"] = "reappearing", 3
        old_reply = next(item for item in json.loads(fixture)["contributions"] if item["id"] == "a")
        old_reply["parent_id"], old_reply["kind"] = parent, kind
        reappearing["contributions"].append(old_reply)
        reappearing_bytes = json.dumps(reappearing).encode()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            importer.import_snapshot(
                reappearing_bytes,
                digest=hashlib.sha256(reappearing_bytes).hexdigest(),
                store="example-store",
                environment="test",
            )
        assert db.db.execute("SELECT snapshot_id FROM review_publications").fetchone()[0] == "removed"
    with pytest.raises(sqlite3.IntegrityError, match="identity links are immutable"):
        db.db.execute("DELETE FROM review_identity_links")


def test_scoped_public_attribution_never_exposes_private_source_identity(monkeypatch):
    db = Database()

    def execute(_flag, filename):
        db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")

    monkeypatch.setattr(importer, "execute", execute)
    data = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
    data["schema"] = "cartray.reviews.snapshot.v3"
    data["source"]["identity"] = "PRIVATE_SOURCE_IDENTITY"
    data["source"]["public_attribution"] = "Example historical reviews"
    for item in data["contributions"]:
        item["title"] = None
        item["date"]["source_local_raw"] = None
        item["date"]["source_utc_raw"] = None
    content = json.dumps(data).encode()
    importer.import_snapshot(
        content, digest=hashlib.sha256(content).hexdigest(), store="example-store", environment="test"
    )
    db.db.execute("INSERT INTO review_subjects VALUES ('example-store','test','example-policy','product','/product/')")
    db.db.execute("INSERT INTO native_publication_versions VALUES ('example-store','test','example-policy',0,0,0,0)")
    response = asyncio.run(
        unified_read(db, "example-store", "test", "example-policy", "contributions", {}, TestResponse)
    )
    assert response.status == 200
    assert all(
        item["source"] == {"kind": "historical", "attribution": "Example historical reviews"}
        for item in response.body["contributions"]
    )
    assert "PRIVATE_SOURCE_IDENTITY" not in json.dumps(response.body)
    db.db.execute(
        "INSERT INTO native_reviews VALUES ('example-store','test','native-id','example-policy',"
        "'private-reviewer','published','native-revision',1735776000)"
    )
    db.db.execute(
        "INSERT INTO native_revisions VALUES ('example-store','test','native-revision','native-id',"
        "'Native',NULL,'Native text',2)"
    )
    db.db.execute("UPDATE native_publication_versions SET version=1,contribution_count=1,rated_count=1,rating_sum=2")
    db.db.execute(
        "INSERT INTO review_identity_links VALUES ('example-store','test','example-policy',"
        "'PRIVATE_SOURCE_IDENTITY','r','r1',?,'native-id','native-revision',?)",
        (hashlib.sha256(content).hexdigest(), "a" * 64),
    )
    linked = asyncio.run(
        unified_read(db, "example-store", "test", "example-policy", "contributions", {}, TestResponse)
    ).body
    item = next(item for item in linked["contributions"] if item["id"] == "n-native-id")
    assert item["source"] == {"kind": "native", "attribution": None}
    assert item["historical_provenance"]["source"] == {
        "kind": "historical",
        "attribution": "Example historical reviews",
    }
    assert "PRIVATE_SOURCE_IDENTITY" not in json.dumps(linked)


def test_linked_native_edit_never_republishes_historical_copy(lifecycle, monkeypatch):
    from unittest.mock import patch

    from test_acquisition_invitations import issue, stores

    db, commerce = stores()
    db.db.execute(
        "INSERT INTO native_publication_versions(store_key,environment,subject_id) VALUES ('store','test','policy')"
    )
    db.db.commit()
    invitation = issue(db, commerce)
    env = SimpleNamespace(
        REVIEWS_DB=db,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
    )
    with patch.object(lifecycle.time, "time", return_value=101):
        created = invoke(
            lifecycle,
            env,
            "/reviews/v1/contributions",
            {"subject_id": "policy", "body": "Original", "rating": 5, "invitation_token": invitation["token"]},
            key="linked-submission",
        )
        assert created.status == 202
        review, revision = created.body["review_id"], created.body["revision_id"]
        approve = "/reviews-admin/v1/contributions/" + review + "/approve"
        assert invoke(lifecycle, env, approve, {"revision_id": revision}, admin=True).status == 200
        original = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
        original["store"] = "store"
        original["schema"] = "cartray.reviews.snapshot.v3"
        original["source"]["public_attribution"] = "Example historical reviews"
        original["subjects"][0].update({"id": "policy", "path": "/policy/"})
        for item in original["contributions"]:
            item["subject_id"] = "policy"
            item["title"] = None
            item["date"]["source_local_raw"] = None
            item["date"]["source_utc_raw"] = None
        original["summaries"][0]["subject_id"] = "policy"
        raw = json.dumps(original).encode()

        def execute(_flag, filename):
            db.db.executescript("BEGIN;\n" + Path(filename).read_text() + "\nCOMMIT;")

        monkeypatch.setattr(importer, "execute", execute)
        source_digest = hashlib.sha256(raw).hexdigest()
        importer.import_snapshot(raw, digest=source_digest, store="store", environment="test")
        db.db.execute(
            "INSERT INTO review_identity_links VALUES ('store','test','policy','synthetic-native','r','r1',?,?,?,?)",
            (source_digest, review, revision, "a" * 64),
        )
        db.db.commit()

        def public(endpoint):
            return asyncio.run(unified_read(db, "store", "test", "policy", endpoint, {}, TestResponse)).body

        initial = public("summary")
        assert initial["rating"] == {"rated_count": 2, "average": 4.5}
        management = "/reviews/v1/manage/" + review + "/"
        edit = invoke(
            lifecycle,
            env,
            management + "edit",
            {
                "management_token": created.body["management_token"],
                "revision_id": revision,
                "update": {"body": "Corrected", "rating": 2},
            },
            key="linked-edit",
        )
        assert edit.status == 200 and edit.body["status"] == "pending"
        pending = public("summary")
        assert pending["contribution_count"] == 2 and pending["rating"] == {"rated_count": 1, "average": 4}
        assert pending["snapshot_id"] != initial["snapshot_id"]
        pending_text = json.dumps(public("contributions"))
        assert "Original" not in pending_text and review not in pending_text
        assert '"id": "h-r"' not in pending_text and '"id": "h-a"' not in pending_text
        assert invoke(lifecycle, env, approve, {"revision_id": edit.body["revision_id"]}, admin=True).status == 200
        approved = public("summary")
        assert approved["contribution_count"] == 3 and approved["rating"] == {"rated_count": 2, "average": 3}
        assert approved["snapshot_id"] != pending["snapshot_id"]
        listing = public("contributions")
        linked = next(item for item in listing["contributions"] if item["id"] == "n-" + review)
        assert linked["body"] == "Corrected" and linked["verification"] == "cartray_verified_acquisition"
        assert linked["source"] == {"kind": "native", "attribution": None}
        assert linked["historical_provenance"]["source"] == {
            "kind": "historical",
            "attribution": "Example historical reviews",
        }
        assert linked["historical_provenance"]["anchors"] == ["review-r"]
        assert linked["replies"][0]["id"] == "h-a"
        withdrawal = invoke(
            lifecycle,
            env,
            management + "withdraw",
            {"management_token": created.body["management_token"], "revision_id": edit.body["revision_id"]},
            key="linked-withdraw",
        )
        assert withdrawal.status == 200
        withdrawn = public("summary")
        assert withdrawn["contribution_count"] == 2 and withdrawn["rating"] == {"rated_count": 1, "average": 4}
        assert withdrawn["snapshot_id"] != approved["snapshot_id"]
        withdrawn_text = json.dumps(public("contributions"))
        assert "Corrected" not in withdrawn_text and review not in withdrawn_text
        assert '"id": "h-r"' not in withdrawn_text and '"id": "h-a"' not in withdrawn_text
