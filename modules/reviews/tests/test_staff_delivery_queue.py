"""Delivery operations reads use real permission validation and scoped SQL."""

import asyncio
import json
import types

from test_native_lifecycle import Database
from test_native_lifecycle import lifecycle as lifecycle
from test_staff_auth_runtime import claims, settings
from test_staff_identity import token


def add_delivery(db, number, *, store="store", state="uncertain"):
    generation, delivery, secret = f"{number:064x}", f"{number + 1000:064x}", f"{number + 2000:064x}"
    db.db.execute(
        "INSERT INTO invitation_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            store,
            "test",
            f"private-order-{number}",
            generation,
            f"request-{number}",
            secret,
            "v1",
            "a" * 64,
            0,
            0,
            delivery,
            state,
            100,
            None,
            None,
        ),
    )
    return generation


def read(module, env, query, assertion):
    request = types.SimpleNamespace(headers={"Cf-Access-Jwt-Assertion": assertion})
    return asyncio.run(module.delivery_queue_read(request, env, query))


def test_operations_queue_requires_independent_permission_and_is_bounded(lifecycle):
    db = Database()
    for number in range(1, 53):
        add_delivery(db, number)
    # Generation IDs are only unique within an acquisition; the cursor must
    # still preserve another acquisition with the same generation ID.
    add_delivery(db, 53)
    db.db.execute(
        "UPDATE invitation_generations SET generation_id=? WHERE acquisition_id=?",
        (f"{1:064x}", "private-order-53"),
    )
    add_delivery(db, 98, state="failed")
    add_delivery(db, 99, state="accepted")
    add_delivery(db, 100, store="other")
    db.db.commit()
    changes = db.db.total_changes
    assertion = token(claims())
    moderator = settings(REVIEWS_DB=db)
    operator = settings(REVIEWS_DB=db, REVIEWS_STAFF_POLICY=json.dumps({"staff-1": ["reviews:operations"]}))
    assert read(lifecycle, moderator, "state=uncertain", assertion).status == 401
    assert (
        read(lifecycle, settings(REVIEWS_DB=db, REVIEWS_ENVIRONMENT="live"), "state=uncertain", assertion).status == 503
    )
    for query in ("", "state=accepted", "state=uncertain&state=failed", "state=uncertain&after=bad"):
        assert read(lifecycle, operator, query, assertion).status == 400
    first = read(lifecycle, operator, "state=uncertain", assertion)
    assert first.status == 200 and first.headers["Cache-Control"] == "no-store"
    assert len(first.body["deliveries"]) == 50 and first.body["next_after"] == f"{1050:064x}"
    assert "private-order" not in str(first.body) and f"{2001:064x}" not in str(first.body)
    next_page = read(lifecycle, operator, "state=uncertain&after=" + first.body["next_after"], assertion)
    assert len(next_page.body["deliveries"]) == 3 and next_page.body["next_after"] is None
    failed = read(lifecycle, operator, "state=failed", assertion)
    assert [row["generation_id"] for row in failed.body["deliveries"]] == [f"{98:064x}"]
    other = settings(
        REVIEWS_DB=db, REVIEWS_STORE_KEY="other", REVIEWS_STAFF_POLICY=json.dumps({"staff-1": ["reviews:operations"]})
    )
    assert [
        row["generation_id"] for row in read(lifecycle, other, "state=uncertain", assertion).body["deliveries"]
    ] == [f"{100:064x}"]
    assert db.db.execute("SELECT count(*) FROM invitation_generations WHERE state='uncertain'").fetchone()[0] == 54
    assert db.db.total_changes == changes
