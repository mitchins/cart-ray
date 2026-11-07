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
    for query in ("", "state=unknown", "state=uncertain&state=failed", "state=uncertain&after=bad"):
        assert read(lifecycle, operator, query, assertion).status == 400
    first = read(lifecycle, operator, "state=uncertain", assertion)
    assert first.status == 200 and first.headers["Cache-Control"] == "no-store"
    assert len(first.body["deliveries"]) == 50 and first.body["next_after"] == f"{1050:064x}"
    assert first.body["deliveries"][0]["acquisition_id"].startswith("private-order-")
    assert f"{2001:064x}" not in str(first.body)
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


def test_provider_evidence_is_operations_scoped_and_excludes_private_fields(lifecycle):
    db = Database()
    generation = add_delivery(db, 1)
    delivery = f"{1001:064x}"
    db.db.execute(
        "INSERT INTO review_provider_deliveries VALUES (?,?,?,?,?,'smtp2go',?,?)",
        ("store", "test", "private-order-1", generation, delivery, "smtp_1", 101),
    )
    db.db.execute(
        "INSERT INTO review_provider_activity VALUES (?,?,?,?,?,?,?,?)",
        ("store", "test", "private-order-1", generation, "smtp_1", "delivered", "1970-01-01T00:02:00Z", 120),
    )
    db.db.commit()
    assertion = token(claims())
    req = types.SimpleNamespace(headers={"Cf-Access-Jwt-Assertion": assertion})
    moderator = settings(REVIEWS_DB=db)
    operator = settings(REVIEWS_DB=db, REVIEWS_STAFF_POLICY=json.dumps({"staff-1": ["reviews:operations"]}))
    assert asyncio.run(lifecycle.provider_evidence_staff_read(req, moderator, "", delivery)).status == 401
    result = asyncio.run(lifecycle.provider_evidence_staff_read(req, operator, "", delivery))
    assert result.status == 200 and result.headers["Cache-Control"] == "no-store"
    assert result.body["acquisition_id"] == "private-order-1"
    assert result.body["provider"]["provider_email_id"] == "smtp_1"
    assert result.body["activity"][0]["event_type"] == "delivered"
    assert "recipient" not in str(result.body) and "token_hash" not in str(result.body)
    assert asyncio.run(lifecycle.provider_evidence_staff_read(req, operator, "x=1", delivery)).status == 400
    other = settings(REVIEWS_DB=db, REVIEWS_STORE_KEY="other",
                     REVIEWS_STAFF_POLICY=json.dumps({"staff-1": ["reviews:operations"]}))
    assert asyncio.run(lifecycle.provider_evidence_staff_read(req, other, "", delivery)).status == 404


def test_operator_acknowledgement_requires_existing_scoped_evidence_and_audits(lifecycle, monkeypatch):
    db = Database()
    generation = add_delivery(db, 1)
    delivery = f"{1001:064x}"
    db.db.execute(
        "INSERT INTO review_provider_identity_discoveries VALUES (?,?,?,?,?,?,?,?,?)",
        ("store", "test", "private-order-1", generation, delivery, "smtp_1", "delivered", "2026-01-01T00:00:00Z", 120),
    )
    db.db.commit()
    env = settings(REVIEWS_DB=db, REVIEWS_STAFF_ORIGIN="https://staff.example")
    monkeypatch.setattr(lifecycle, "random_token", lambda: "f" * 64)
    request = types.SimpleNamespace(headers={"Origin": "https://staff.example"})
    evidence = {"provider_email_id": "smtp_1", "event_type": "delivered", "event_at": "2026-01-01T00:00:00Z"}
    def action(req, ident, payload):
        return asyncio.run(lifecycle.provider_operations(req, env, ident, "acknowledge", "staff-1", payload))
    assert action(types.SimpleNamespace(headers={}), delivery, evidence).status == 403
    assert action(request, f"{1002:064x}", evidence).status == 404
    assert action(request, delivery, dict(evidence, event_type="rejected")).status == 409
    assert action(request, delivery, evidence).status == 200
    assert tuple(db.db.execute("SELECT actor,action,event_type FROM review_provider_operations_audit").fetchone()) == (
        "staff-1", "evidence_acknowledged", "delivered"
    )
    assert db.db.execute("SELECT state FROM invitation_generations").fetchone()[0] == "uncertain"


def test_refresh_is_audited_scoped_and_never_sends_or_mutates(lifecycle, monkeypatch):
    db = Database()
    generation = add_delivery(db, 1)
    delivery = f"{1001:064x}"
    db.db.commit()
    env = settings(REVIEWS_DB=db, REVIEWS_STAFF_ORIGIN="https://staff.example",
                   REVIEWS_RECONCILIATION=object(), RECONCILIATION_TOKEN="z" * 40)
    monkeypatch.setattr(lifecycle, "random_token", lambda: "f" * 64)
    calls = []

    async def refresh(service, token_value, target, acquisition, gen, ident):
        calls.append((target, acquisition, gen, ident))
        return 200

    monkeypatch.setattr(lifecycle, "request_provider_refresh", refresh)
    request = types.SimpleNamespace(headers={"Origin": "https://staff.example"})
    response = asyncio.run(lifecycle.provider_operations(request, env, delivery, "refresh", "staff-1", {}))
    assert response.status == 200
    assert calls == [("discover", "private-order-1", generation, delivery)]
    assert db.db.execute("SELECT action FROM review_provider_operations_audit").fetchone()[0] == "refresh_requested"
    assert db.db.execute("SELECT state FROM invitation_generations").fetchone()[0] == "uncertain"
    result = asyncio.run(lifecycle.provider_operations(request, env, delivery, "refresh", "staff-1", {"x": 1}))
    assert result.status == 400
