"""Native staff replies preserve immutable history and review aggregates."""

import asyncio
import sqlite3
import types

import pytest
from cartray_reviews.management import management_token
from cartray_reviews.unified import unified_read
from test_native_lifecycle import Database, TestResponse, invoke
from test_native_lifecycle import lifecycle as lifecycle


def setup_review(module):
    db = Database()
    env = types.SimpleNamespace(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="test", REVIEWS_TEST_ADMIN_TOKEN="s" * 40
    )
    assert (
        invoke(
            module,
            env,
            "/reviews-admin/v1/subjects",
            {"subject_id": "policy", "kind": "product", "path": "/policy/"},
            admin=True,
        ).status
        == 201
    )
    proof = invoke(
        module,
        env,
        "/reviews-admin/v1/test-capabilities",
        {"subject_id": "policy", "test_reviewer": "guest"},
        admin=True,
    ).body["test_capability"]
    review = invoke(
        module,
        env,
        "/reviews/v1/contributions",
        {"subject_id": "policy", "author": "Guest", "body": "Good", "rating": 2, "test_capability": proof},
        key="submit",
    ).body
    base = "/reviews-admin/v1/contributions/" + review["review_id"] + "/"
    assert invoke(module, env, base + "approve", {"revision_id": review["revision_id"]}, admin=True).status == 200
    return db, env, review, base


def read(module, db):
    version = db.db.execute("SELECT version FROM native_publication_versions WHERE subject_id='policy'").fetchone()[0]
    return asyncio.run(module.native_read(db, "store", "test", "policy", "contributions", {}, version))


def test_reply_lifecycle_is_idempotent_audited_and_does_not_change_ratings(lifecycle):
    db, env, review, base = setup_review(lifecycle)
    payload = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": None,
        "author": "Store",
        "body": "Thank you",
    }
    assert invoke(lifecycle, env, base + "reply", payload, key="create").status == 401
    created = invoke(lifecycle, env, base + "reply", payload, admin=True, key="create")
    assert created.status == 200
    assert invoke(lifecycle, env, base + "reply", payload, admin=True, key="create").body == created.body
    assert (
        invoke(lifecycle, env, base + "reply", payload | {"body": "Different"}, admin=True, key="create").status == 409
    )
    page = read(lifecycle, db).body
    reply = page["contributions"][0]["replies"][0]
    assert reply["body"] == "Thank you" and reply["rating"] is None and reply["id"] == created.body["reply_id"]
    assert "test_admin" not in str(page)
    previous = created.body["reply_revision_id"]
    edited = invoke(
        lifecycle,
        env,
        base + "reply",
        payload | {"expected_reply_revision_id": previous, "body": "Updated"},
        admin=True,
        key="edit",
    )
    assert edited.status == 200 and edited.body["reply_revision_id"] != previous
    assert read(lifecycle, db).body["contributions"][0]["replies"][0]["body"] == "Updated"
    assert (
        invoke(
            lifecycle, env, base + "reply", payload | {"expected_reply_revision_id": previous}, admin=True, key="stale"
        ).status
        == 409
    )
    withdrawal = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": edited.body["reply_revision_id"],
    }
    withdrawn = invoke(lifecycle, env, base + "reply-withdraw", withdrawal, admin=True, key="withdraw")
    assert withdrawn.status == 200
    assert (
        invoke(lifecycle, env, base + "reply-withdraw", withdrawal, admin=True, key="withdraw").body == withdrawn.body
    )
    assert read(lifecycle, db).body["contributions"][0]["replies"] == []
    aggregates = db.db.execute(
        "SELECT contribution_count,rated_count,rating_sum FROM native_publication_versions"
    ).fetchone()
    assert tuple(aggregates) == (1, 1, 2)
    events = db.db.execute("SELECT action,actor FROM native_staff_reply_audit ORDER BY rowid").fetchall()
    assert [tuple(event) for event in events] == [
        ("created", "test_admin"),
        ("edited", "test_admin"),
        ("withdrawn", "test_admin"),
    ]
    assert db.db.execute("SELECT count(*) FROM native_staff_reply_revisions").fetchone()[0] == 2
    assert db.db.execute("SELECT count(*) FROM native_effect_outbox").fetchone()[0] == 4
    staff_request = types.SimpleNamespace(headers={"Authorization": "Bearer " + env.REVIEWS_TEST_ADMIN_TOKEN})
    history = asyncio.run(lifecycle.staff_history_read(staff_request, env, "", review["review_id"], "reply-revisions"))
    assert history.status == 200 and len(history.body["reply_revisions"]) == 2
    assert history.body["reply"]["state"] == "withdrawn"
    audit = asyncio.run(lifecycle.staff_history_read(staff_request, env, "", review["review_id"], "reply-audit"))
    assert {event["action"] for event in audit.body["reply_audit"]} == {"created", "edited", "withdrawn"}
    try:
        db.db.execute("UPDATE native_staff_reply_revisions SET body='changed'")
        assert False, "reply revisions must be immutable"
    except sqlite3.IntegrityError:
        pass


def test_parent_withdrawal_hides_reply_and_stale_or_cross_store_writes_fail(lifecycle):
    db, env, review, base = setup_review(lifecycle)
    payload = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": None,
        "author": "Store",
        "body": "Thank you",
    }
    assert (
        invoke(
            lifecycle,
            types.SimpleNamespace(**(vars(env) | {"REVIEWS_STORE_KEY": "other"})),
            base + "reply",
            payload,
            admin=True,
            key="other",
        ).status
        == 409
    )
    assert invoke(lifecycle, env, base + "reply", payload, admin=True, key="create").status == 200
    assert invoke(lifecycle, env, base + "withdraw", {"revision_id": review["revision_id"]}, admin=True).status == 200
    assert read(lifecycle, db).body["contributions"] == []
    assert invoke(lifecycle, env, base + "reply", payload, admin=True, key="new").status == 409


def test_concurrent_reply_edits_have_one_winner(lifecycle):
    db, env, review, base = setup_review(lifecycle)
    payload = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": None,
        "author": "Store",
        "body": "Original",
    }
    created = invoke(lifecycle, env, base + "reply", payload, admin=True, key="create").body
    real_batch = db.batch

    async def exercise():
        gate, arrived = asyncio.Event(), 0

        async def simultaneous(statements):
            nonlocal arrived
            if any("INSERT INTO native_staff_reply_requests" in statement.sql for statement in statements):
                arrived += 1
                if arrived == 2:
                    gate.set()
                await gate.wait()
            return await real_batch(statements)

        db.batch = simultaneous

        def request(key, body):
            return types.SimpleNamespace(
                method="POST",
                headers={"Authorization": "Bearer " + env.REVIEWS_TEST_ADMIN_TOKEN, "Idempotency-Key": key},
                data=body,
                body=None,
            )

        previous = created["reply_revision_id"]
        return await asyncio.gather(
            *[
                lifecycle.handle_write(
                    request(
                        "race-" + str(i), payload | {"expected_reply_revision_id": previous, "body": "Edit " + str(i)}
                    ),
                    env,
                    base + "reply",
                )
                for i in range(2)
            ]
        )

    results = asyncio.run(exercise())
    assert sorted(result.status for result in results) == [200, 409]
    assert db.db.execute("SELECT count(*) FROM native_staff_reply_audit").fetchone()[0] == 2
    assert db.db.execute("SELECT count(*) FROM native_staff_reply_revisions").fetchone()[0] == 2


def test_reply_cannot_publish_after_concurrent_parent_withdrawal(lifecycle):
    db, env, review, base = setup_review(lifecycle)
    payload = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": None,
        "author": "Store",
        "body": "Thank you",
    }
    real_batch = db.batch

    async def exercise():
        reached, release = asyncio.Event(), asyncio.Event()

        async def pause_reply(statements):
            if any("INSERT INTO native_staff_reply_requests" in statement.sql for statement in statements):
                reached.set()
                await release.wait()
            return await real_batch(statements)

        db.batch = pause_reply
        headers = {"Authorization": "Bearer " + env.REVIEWS_TEST_ADMIN_TOKEN, "Idempotency-Key": "create"}
        reply_request = types.SimpleNamespace(method="POST", headers=headers, data=payload, body=None)
        withdrawal = types.SimpleNamespace(
            method="POST", headers=headers, data={"revision_id": review["revision_id"]}, body=None
        )
        pending = asyncio.create_task(lifecycle.handle_write(reply_request, env, base + "reply"))
        await reached.wait()
        withdrawn = await lifecycle.handle_write(withdrawal, env, base + "withdraw")
        release.set()
        return withdrawn, await pending

    withdrawn, reply = asyncio.run(exercise())
    assert withdrawn.status == 200 and reply.status == 409
    assert db.db.execute("SELECT count(*) FROM native_staff_reply_audit").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM native_staff_replies").fetchone()[0] == 0


def test_reply_is_not_reused_after_parent_revision_changes(lifecycle):
    db, env, review, base = setup_review(lifecycle)
    payload = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": None,
        "author": "Store",
        "body": "For the original review",
    }
    assert invoke(lifecycle, env, base + "reply", payload, admin=True, key="create").status == 200
    db.db.execute(
        "INSERT INTO native_review_acquisitions VALUES (?,?,?,?,?)",
        ("store", "test", review["review_id"], "synthetic-purchase", "policy"),
    )
    db.db.commit()
    env.REVIEWS_MODE = "verified_acquisition_only"
    env.REVIEWS_REVIEWER_KEY = "r" * 40
    env.REVIEWS_INVITATION_KEY = "i" * 40
    env.REVIEWS_SETTLEMENT_TOKEN = "u" * 40
    env.CARTRAY_DB = db
    capability = management_token(
        env.REVIEWS_REVIEWER_KEY.encode(), store="store", environment="test", review_id=review["review_id"]
    )
    edited = invoke(
        lifecycle,
        env,
        "/reviews/v1/manage/" + review["review_id"] + "/edit",
        {
            "management_token": capability,
            "revision_id": review["revision_id"],
            "update": {"author": "Guest", "body": "Changed review", "rating": 2},
        },
        key="customer-edit",
    )
    assert edited.status == 200 and edited.body["status"] == "pending"
    assert read(lifecycle, db).body["contributions"] == []
    assert (
        invoke(lifecycle, env, base + "approve", {"revision_id": edited.body["revision_id"]}, admin=True).status == 200
    )
    assert read(lifecycle, db).body["contributions"][0]["replies"] == []


@pytest.mark.parametrize("read_kind", ["native", "unified"])
@pytest.mark.parametrize("change", ["reply-edit", "parent-withdraw"])
def test_public_read_fails_closed_when_content_changes_mid_read(lifecycle, read_kind, change):
    db, env, review, base = setup_review(lifecycle)
    payload = {
        "parent_revision_id": review["revision_id"],
        "expected_reply_revision_id": None,
        "author": "Store",
        "body": "Original reply",
    }
    created = invoke(lifecycle, env, base + "reply", payload, admin=True, key="create").body
    if read_kind == "unified":
        db.db.execute("INSERT INTO review_snapshots VALUES ('store','test','one',1,?)", ("a" * 64,))
        db.db.execute(
            "INSERT INTO review_snapshot_subjects VALUES ('store','test','one','policy','product','/policy/')"
        )
        db.db.execute("INSERT INTO review_publications VALUES ('store','test','one',1)")
        db.db.commit()
    version = db.db.execute("SELECT version FROM native_publication_versions WHERE subject_id='policy'").fetchone()[0]
    original_prepare = db.prepare

    async def exercise():
        reached, release = asyncio.Event(), asyncio.Event()

        def prepare(sql):
            statement = original_prepare(sql)
            if sql.startswith("SELECT h.review_id,h.reply_id,v.author,v.body,v.created_at"):
                original_all = statement.all

                async def paused_all():
                    reached.set()
                    await release.wait()
                    return await original_all()

                statement.all = paused_all
            return statement

        db.prepare = prepare
        if read_kind == "native":
            reading = asyncio.create_task(
                lifecycle.native_read(db, "store", "test", "policy", "contributions", {}, version)
            )
        else:
            reading = asyncio.create_task(
                unified_read(db, "store", "test", "policy", "contributions", {}, TestResponse)
            )
        await asyncio.wait_for(reached.wait(), timeout=2)
        if change == "reply-edit":
            path = base + "reply"
            data = payload | {"expected_reply_revision_id": created["reply_revision_id"], "body": "New reply"}
        else:
            path = base + "withdraw"
            data = {"revision_id": review["revision_id"]}
        request = types.SimpleNamespace(
            method="POST",
            headers={"Authorization": "Bearer " + env.REVIEWS_TEST_ADMIN_TOKEN, "Idempotency-Key": "race"},
            data=data,
            body=None,
        )
        changed = await lifecycle.handle_write(request, env, path)
        release.set()
        return changed, await reading

    changed, response = asyncio.run(exercise())
    assert changed.status == 200
    assert response.status == 409
