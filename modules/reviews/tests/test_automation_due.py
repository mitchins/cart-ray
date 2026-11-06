"""Real SQLite exercises keyed lease fencing; no claim authorizes sending."""

import asyncio

import pytest
from cartray_reviews.automation_due import claim_due, complete_claim
from test_automation_intake import run, setup

A, B = "a" * 64, "b" * 64


def claim(db, token=A, now=110, **kw):
    result = asyncio.run(
        claim_due(
            db,
            store=kw.get("store", "store"),
            environment=kw.get("environment", "test"),
            acquisition=kw.get("acquisition", "order_1"),
            token=token,
            now=now,
            lease_seconds=kw.get("lease_seconds", 10),
        )
    )
    db.db.commit()
    return result


def finish(db, token=A, generation=1, now=111):
    result = asyncio.run(
        complete_claim(
            db, store="store", environment="test", acquisition="order_1", token=token, generation=generation, now=now
        )
    )
    db.db.commit()
    return result


def test_due_replay_takeover_and_fencing():
    checkout, db = setup()
    run(checkout, db)
    assert claim(db, now=109) is None
    assert claim(db) == {"generation": 1, "lease_until": 120}
    assert claim(db, now=111) == {"generation": 1, "lease_until": 120}
    assert claim(db, B, now=119) is None
    assert claim(db, B, now=120) == {"generation": 2, "lease_until": 130}
    assert not finish(db, now=121)
    assert finish(db, B, 2, 121)
    assert claim(db, A, now=131) is None
    assert (
        db.db.execute("SELECT state FROM review_automation_intents").fetchone()[0]
        == "held_eligibility_recipient_consent"
    )
    assert db.db.execute("SELECT count(*) FROM invitation_generations").fetchone()[0] == 0


def test_scope_and_untrusted_parameters():
    checkout, db = setup()
    run(checkout, db)
    assert claim(db, store="other") is None
    assert claim(db, acquisition="other") is None
    for kwargs in ({"environment": "live"}, {"lease_seconds": 301}, {"token": "bad"}, {"now": True}):
        with pytest.raises(ValueError):
            claim(db, **kwargs)


def test_expiry_and_generation_validation():
    checkout, db = setup()
    run(checkout, db)
    claim(db)
    assert not finish(db, now=120)
    with pytest.raises(ValueError):
        finish(db, generation=0)
    with pytest.raises(ValueError):
        claim(db, lease_seconds=0)
    assert claim(db, B, now=120)["generation"] == 2
    assert not finish(db, B, generation=1, now=121)
    assert finish(db, B, generation=2, now=121)
