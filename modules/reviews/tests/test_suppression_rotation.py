"""Offline, fenced suppression-key migration preserves old-link withdrawals."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import pytest
from cartray_reviews.automation_initial import unsubscribe, unsubscribe_token
from cartray_reviews.automation_recipient import resolve_replay
from cartray_reviews.suppression_rotation import activate, backfill, freeze, switch_context
from test_automation_intake import Database, run, setup
from test_automation_recipient import bind, consent

ROOT = Path(__file__).resolve().parents[1]
OLD, NEW, OTHER = "a" * 40, "b" * 40, "c" * 40
EMAIL = "Client@example.com"


def digest(key, email=EMAIL):
    message = json.dumps(["acorn-recipient-v2", "store", "test", email], separators=(",", ":"))
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def scope():
    return {"store": "store", "environment": "test", "old": "key1", "target": "key2"}


def bind_legacy(reviews):
    reviews.db.execute(
        "INSERT INTO review_automation_recipient_bindings VALUES (?,?,?,?,?,'v2',?,100)",
        ("store", "test", "order_1", 10, digest(OLD), "purchase_contact_10"),
    )
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,'review_invitation','synthetic-consent',100,NULL)",
        ("store", "test", "order_1", digest(OLD)),
    )
    reviews.db.commit()


def context(version, key):
    ctx = Database(ROOT / "consent" / "migrations")
    check = hmac.new(key.encode(), json.dumps(
        ["reviews-suppression-key-check-v1", "store", "test", "install", version], separators=(",", ":")
    ).encode(), hashlib.sha256).hexdigest()
    ctx.db.execute(
        "INSERT INTO review_suppression_key_registry VALUES (?,?,?,?,?)", ("store", "test", "install", version, check)
    )
    ctx.db.execute("INSERT INTO review_suppression_key_current VALUES (?,?,?,?)", ("store", "test", "install", version))
    ctx.db.commit()
    return ctx


def test_complete_rotation_preserves_suppression_and_old_link_withdrawal():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind_legacy(reviews)
    ctx = context("key1", OLD)
    asyncio.run(freeze(reviews, **scope()))
    # Old/new sources cannot add a binding while the atomic freeze is active.
    with pytest.raises(sqlite3.IntegrityError):
        reviews.db.execute(
            "INSERT INTO review_automation_recipient_bindings VALUES (?,?,?,?,?,'v2',?,100)",
            ("store", "test", "other", 11, digest(OLD), "contact"),
        )
    reviews.db.rollback()
    with pytest.raises(ValueError):
        asyncio.run(switch_context(reviews, ctx, installation="install", old_key=OLD, new_key=NEW, **scope()))
    with pytest.raises(ValueError):
        asyncio.run(backfill(reviews, **scope(), old_key=OTHER, new_key=NEW, contacts=[EMAIL], now=120))
    asyncio.run(backfill(reviews, **scope(), old_key=OLD, new_key=NEW, contacts=[EMAIL], now=120))
    # A later chunk or switch cannot substitute another target key under the
    # same version after aliases have been generated.
    with pytest.raises(ValueError):
        asyncio.run(backfill(reviews, **scope(), old_key=OLD, new_key=OTHER, contacts=[EMAIL], now=120))
    with pytest.raises(ValueError):
        asyncio.run(switch_context(reviews, ctx, installation="install", old_key=OLD, new_key=OTHER, **scope()))
    # An old invitation can be unsubscribed during the fence, after backfill.
    token = unsubscribe_token(secret=b"u" * 40, store="store", environment="test", outbox_id=10)
    asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=b"u" * 40, now=120))
    assert {r[0] for r in reviews.db.execute("SELECT recipient_context_hash FROM review_recipient_suppressions")} == {
        digest(OLD), digest(NEW)
    }
    with pytest.raises(ValueError):
        asyncio.run(activate(reviews, ctx, installation="install", new_key=NEW, **scope()))
    asyncio.run(switch_context(reviews, ctx, installation="install", old_key=OLD, new_key=NEW, **scope()))
    # Retrying an uncertain context switch reuses the immutable target epoch.
    asyncio.run(switch_context(reviews, ctx, installation="install", old_key=OLD, new_key=NEW, **scope()))
    asyncio.run(activate(reviews, ctx, installation="install", new_key=NEW, **scope()))
    asyncio.run(activate(reviews, ctx, installation="install", new_key=NEW, **scope()))
    state = reviews.db.execute("SELECT state,active_binding_version FROM review_suppression_rotation").fetchone()
    assert tuple(state) == ("active", "v2_key2")
    # Authenticated retry of the immutable old evidence does not require a new
    # insert and remains idempotent despite the retired-writer fence.
    bind(reviews, data={"outbox_id": 10, "recipient_context_hash": digest(OLD), "binding_version": "v2",
                        "evidence_reference": "purchase_contact_10", "verified_at": 100})
    with pytest.raises(ValueError):
        consent(checkout, reviews, recipient=digest(OLD))
    with pytest.raises(sqlite3.IntegrityError):
        reviews.db.execute(
            "INSERT INTO review_automation_recipient_bindings VALUES (?,?,?,?,?,'v2',?,100)",
            ("store", "test", "old-writer", 99, digest(OLD), "contact"),
        )
    reviews.db.rollback()
    # Unsubscription from the old link stays idempotent after old key retirement.
    asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=b"u" * 40, now=121))
    assert reviews.db.execute("SELECT count(*) FROM review_recipient_suppressions").fetchone()[0] == 2


def test_existing_suppression_is_backfilled_and_unknown_contacts_fail_closed():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind_legacy(reviews)
    asyncio.run(freeze(reviews, **scope()))
    # Customers can still unsubscribe while writes and sends are fenced.
    token = unsubscribe_token(secret=b"u" * 40, store="store", environment="test", outbox_id=10)
    asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=b"u" * 40, now=120))
    with pytest.raises(ValueError):
        asyncio.run(backfill(reviews, **scope(), old_key=OLD, new_key=NEW, contacts=["wrong@example.com"], now=121))
    asyncio.run(backfill(reviews, **scope(), old_key=OLD, new_key=NEW, contacts=[EMAIL], now=121))
    assert {r[0] for r in reviews.db.execute("SELECT recipient_context_hash FROM review_recipient_suppressions")} == {
        digest(OLD), digest(NEW)
    }


def test_first_withdrawal_after_cutover_and_old_outbox_replay_are_safe():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind_legacy(reviews)
    ctx = context("key1", OLD)
    asyncio.run(freeze(reviews, **scope()))
    asyncio.run(backfill(reviews, **scope(), old_key=OLD, new_key=NEW, contacts=[EMAIL], now=120))
    asyncio.run(switch_context(reviews, ctx, installation="install", old_key=OLD, new_key=NEW, **scope()))
    asyncio.run(activate(reviews, ctx, installation="install", new_key=NEW, **scope()))
    replay = asyncio.run(
        resolve_replay(reviews, store="store", environment="test", outbox_id=10, candidate=digest(NEW))
    )
    assert replay["status"] == "existing" and replay["recipient_context_hash"] == digest(OLD)
    assert replay["binding_version"] == "v2" and replay["consent_reference"] == "synthetic-consent"
    consent(checkout, reviews, recipient=digest(OLD))
    with pytest.raises(ValueError):
        asyncio.run(resolve_replay(reviews, store="store", environment="test", outbox_id=10, candidate=digest(OTHER)))
    token = unsubscribe_token(secret=b"u" * 40, store="store", environment="test", outbox_id=10)
    asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=b"u" * 40, now=121))
    assert {r[0] for r in reviews.db.execute("SELECT recipient_context_hash FROM review_recipient_suppressions")} == {
        digest(OLD), digest(NEW)
    }
    assert asyncio.run(resolve_replay(reviews, store="store", environment="test", outbox_id=10,
                                      candidate=digest(NEW))) == {"status": "suppressed"}
    with pytest.raises(ValueError):
        consent(checkout, reviews, recipient=digest(OLD))


def test_freeze_and_provider_claim_are_mutually_exclusive():
    import secrets

    from cartray_reviews.invitations import claim_delivery, prepare_initial_delivery
    from test_acquisition_invitations import stores

    def draft():
        reviews, commerce = stores()
        item = asyncio.run(prepare_initial_delivery(
            reviews, commerce, store="store", environment="test", event_id="event_1",
            subject_maps={"v1": {"product_1": "policy"}}, recipient_context_hash="a" * 64,
            request_key="initial", key_version="v1", secret=b"z" * 32, now=100,
            random_token=lambda: secrets.token_hex(32),
        ))
        return reviews, {"store": "store", "environment": "test", "acquisition": "order_1",
                         "generation": item["generation_id"], "delivery": item["delivery_identity"], "now": 101}

    reviews, claim = draft()
    asyncio.run(freeze(reviews, **scope()))
    with pytest.raises(ValueError):
        asyncio.run(claim_delivery(reviews, **claim))
    reviews, claim = draft()
    assert asyncio.run(claim_delivery(reviews, **claim)) == {"state": "dispatching"}
    with pytest.raises(ValueError):
        asyncio.run(freeze(reviews, **scope()))


def test_database_fans_out_tombstone_from_an_old_worker():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind_legacy(reviews)
    ctx = context("key1", OLD)
    asyncio.run(freeze(reviews, **scope()))
    asyncio.run(backfill(reviews, **scope(), old_key=OLD, new_key=NEW, contacts=[EMAIL], now=120))
    asyncio.run(switch_context(reviews, ctx, installation="install", old_key=OLD, new_key=NEW, **scope()))
    asyncio.run(activate(reviews, ctx, installation="install", new_key=NEW, **scope()))
    reviews.db.execute(
        "INSERT INTO review_recipient_suppressions VALUES (?,?,?,'review_invitation',?)",
        ("store", "test", digest(OLD), 121),
    )
    rows = reviews.db.execute("SELECT recipient_context_hash FROM review_recipient_suppressions")
    assert {row[0] for row in rows} == {digest(OLD), digest(NEW)}
