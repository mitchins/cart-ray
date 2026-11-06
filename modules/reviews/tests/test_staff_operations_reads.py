"""Staff read endpoints exercise real scoped SQLite queries and authorization."""

import asyncio
import json
import types

from test_native_lifecycle import Database
from test_native_lifecycle import lifecycle as lifecycle

TOKEN = "t" * 40


def request(token=TOKEN):
    return types.SimpleNamespace(headers={"Authorization": "Bearer " + token})


def environment(db, store="store", mode="test"):
    return types.SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY=store, REVIEWS_ENVIRONMENT=mode, REVIEWS_TEST_ADMIN_TOKEN=TOKEN
    )


def add_review(db, number, *, store="store", state="published"):
    rid, revision = f"{number:064x}", f"{number + 1000:064x}"
    db.db.execute(
        "INSERT INTO native_reviews VALUES(?,?,?,?,?,?,?,?)",
        (store, "test", rid, "policy", f"private-reviewer-{number}", state, revision, 10),
    )
    db.db.execute(
        "INSERT INTO native_revisions VALUES(?,?,?,?,?,?,?,?)",
        (store, "test", revision, rid, "Synthetic", None, "Synthetic body", 2),
    )
    return rid, revision


def read_published(module, env, query="", token=TOKEN):
    return asyncio.run(module.published_staff_read(request(token), env, query))


def read_history(module, env, rid, kind, query="", token=TOKEN):
    return asyncio.run(module.staff_history_read(request(token), env, query, rid, kind))


def test_published_is_scoped_bounded_and_only_published(lifecycle):
    db = Database()
    for number in range(1, 53):
        add_review(db, number)
    add_review(db, 99, state="pending")
    add_review(db, 100, store="other")
    db.db.commit()
    env = environment(db)
    assert read_published(lifecycle, env, token="bad").status == 401
    assert read_published(lifecycle, environment(db, mode="live")).status == 503
    for query in ("after=bad", "after=a&after=b", "other=1"):
        assert read_published(lifecycle, env, query).status == 400
    first = read_published(lifecycle, env)
    assert first.status == 200 and first.headers["Cache-Control"] == "no-store"
    assert len(first.body["published"]) == 50 and first.body["next_after"] == f"{50:064x}"
    assert first.body["published"][0]["subject_name"] is None
    assert "private-reviewer" not in str(first.body)
    second = read_published(lifecycle, env, "after=" + first.body["next_after"])
    assert len(second.body["published"]) == 2 and second.body["next_after"] is None
    assert read_published(lifecycle, environment(db, store="other")).body["published"][0]["review_id"] == f"{100:064x}"


def test_scoped_revision_and_audit_history(lifecycle):
    db = Database()
    rid, revision = add_review(db, 1)
    other, other_revision = add_review(db, 2, store="other")
    for index in range(1, 53):
        db.db.execute(
            "INSERT INTO native_audit VALUES(?,?,?,?,?,?,?,?)",
            (f"{index:064x}", "store", "test", rid, revision, "approve", "staff:actor", 99),
        )
    db.db.execute(
        "INSERT INTO native_audit VALUES(?,?,?,?,?,?,?,?)",
        ("f" * 64, "other", "test", other, other_revision, "approve", "private-actor", 99),
    )
    db.db.commit()
    env = environment(db)
    assert read_history(lifecycle, env, rid, "audit", token="bad").status == 401
    assert read_history(lifecycle, environment(db, store="other"), rid, "audit").status == 404
    assert read_history(lifecycle, env, rid, "audit", "after=bad").status == 400
    revisions = read_history(lifecycle, env, rid, "revisions")
    assert revisions.body["current_revision_id"] == revision
    assert revisions.body["revisions"] == [
        {"revision_id": revision, "author": "Synthetic", "title": None, "body": "Synthetic body", "rating": 2}
    ]
    first = read_history(lifecycle, env, rid, "audit")
    assert len(first.body["audit"]) == 50 and first.body["next_after"] == f"{50:064x}"
    assert "private-actor" not in str(first.body)
    second = read_history(lifecycle, env, rid, "audit", "after=" + first.body["next_after"])
    assert len(second.body["audit"]) == 2 and second.body["next_after"] is None


def test_staff_page_byte_limit_trims_without_losing_cursor(lifecycle):
    db = Database()
    for number in range(1, 21):
        _, revision = add_review(db, number)
        db.db.execute("UPDATE native_revisions SET body=? WHERE revision_id=?", ("x" * 10000, revision))
    db.db.commit()
    env = environment(db)
    first = read_published(lifecycle, env)
    first_ids = [item["review_id"] for item in first.body["published"]]
    assert 0 < len(first_ids) < 20
    assert first.body["next_after"] == first_ids[-1]
    assert len(json.dumps(first.body, ensure_ascii=True).encode("ascii")) <= 128000
    second = read_published(lifecycle, env, "after=" + first.body["next_after"])
    second_ids = [item["review_id"] for item in second.body["published"]]
    assert first_ids + second_ids == [f"{number:064x}" for number in range(1, 21)]
    assert second.body["next_after"] is None
