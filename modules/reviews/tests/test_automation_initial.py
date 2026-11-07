"""Permission, due time and unsubscribe use the real Reviews SQL schema."""

import asyncio

import pytest
from cartray_reviews.automation_initial import due_initial, unsubscribe, unsubscribe_token
from test_automation_intake import run, setup
from test_automation_recipient import RECIPIENT, bind, consent

KEY = b"unsubscribe-synthetic-secret-0123456789"


def due(db, now=120):
    return asyncio.run(due_initial(db, store="store", environment="test", outbox_id=10, now=now))


def test_due_requires_time_matching_contact_and_permission_and_unsubscribe_is_idempotent():
    checkout, reviews = setup()
    run(checkout, reviews)
    with pytest.raises(ValueError):
        due(reviews)
    bind(
        reviews,
        data={
            "outbox_id": 10,
            "recipient_context_hash": RECIPIENT,
            "binding_version": "v2",
            "evidence_reference": "purchase_contact_10",
            "verified_at": 100,
        },
    )
    with pytest.raises(ValueError):
        due(reviews)
    consent(checkout, reviews)
    with pytest.raises(ValueError):
        due(reviews, now=109)
    assert due(reviews)["acquisition_id"] == "order_1"
    token = unsubscribe_token(secret=KEY, store="store", environment="test", outbox_id=10)
    with pytest.raises(ValueError):
        asyncio.run(
            unsubscribe(reviews, store="store", environment="test", token=token[:-1] + "f", secret=KEY, now=120)
        )
    for now in (120, 121):
        asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=KEY, now=now))
    assert reviews.db.execute("SELECT count(*) FROM review_invitation_consent_withdrawals").fetchone()[0] == 1
    with pytest.raises(ValueError):
        due(reviews)
    with pytest.raises(ValueError):
        consent(checkout, reviews)


def test_unsubscribe_suppresses_same_merchant_future_purchase_but_not_another_merchant():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(
        reviews,
        data={
            "outbox_id": 10,
            "recipient_context_hash": RECIPIENT,
            "binding_version": "v2",
            "evidence_reference": "purchase_contact_10",
            "verified_at": 100,
        },
    )
    consent(checkout, reviews)
    db = reviews.db
    for store, outbox, acquisition in [("store", 11, "order_2"), ("other", 12, "order_3")]:
        db.execute(
            "INSERT INTO review_automation_intake VALUES (?,?,?,?,?,100,110,'v1','accepted')",
            (store, "test", outbox, acquisition, "a" * 64),
        )
        db.execute(
            "INSERT INTO review_automation_intents VALUES (?,?,?,?,110,'v1','held_eligibility_recipient_consent')",
            (store, "test", acquisition, outbox),
        )
        db.execute(
            "INSERT INTO review_automation_recipient_bindings VALUES (?,?,?,?,?,'v2',?,100)",
            (store, "test", acquisition, outbox, RECIPIENT, "purchase_contact_" + str(outbox)),
        )
        db.execute(
            "INSERT INTO review_invitation_consents VALUES (?,?,?,?,'review_invitation','grant',100,NULL)",
            (store, "test", acquisition, RECIPIENT),
        )
    db.commit()

    def check(store, outbox):
        return asyncio.run(due_initial(reviews, store=store, environment="test", outbox_id=outbox, now=120))

    assert check("store", 11)["acquisition_id"] == "order_2"
    token = unsubscribe_token(secret=KEY, store="store", environment="test", outbox_id=10)
    asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=KEY, now=120))
    with pytest.raises(ValueError):
        check("store", 11)
    assert check("other", 12)["acquisition_id"] == "order_3"
    assert db.execute("SELECT count(*) FROM review_recipient_suppressions").fetchone()[0] == 1


def test_order_scoped_legacy_binding_cannot_claim_merchant_wide_unsubscribe():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(reviews)
    consent(checkout, reviews)
    token = unsubscribe_token(secret=KEY, store="store", environment="test", outbox_id=10)
    with pytest.raises(ValueError):
        asyncio.run(unsubscribe(reviews, store="store", environment="test", token=token, secret=KEY, now=120))
    assert reviews.db.execute("SELECT count(*) FROM review_recipient_suppressions").fetchone()[0] == 0


def test_merchant_suppression_fences_claim_and_inflight_acceptance():
    import secrets

    from cartray_reviews.invitations import claim_delivery, prepare_initial_delivery, record_delivery_outcome
    from test_acquisition_invitations import stores

    def draft_and_claim():
        reviews, commerce = stores()
        draft = asyncio.run(
            prepare_initial_delivery(
                reviews,
                commerce,
                store="store",
                environment="test",
                event_id="event_1",
                subject_maps={"v1": {"product_1": "policy"}},
                recipient_context_hash="a" * 64,
                request_key="initial",
                key_version="v1",
                secret=b"z" * 32,
                now=100,
                random_token=lambda: secrets.token_hex(32),
            )
        )
        claim = dict(
            store="store",
            environment="test",
            acquisition="order_1",
            generation=draft["generation_id"],
            delivery=draft["delivery_identity"],
            now=101,
        )
        return reviews, claim

    reviews, claim = draft_and_claim()
    reviews.db.execute(
        "INSERT INTO review_recipient_suppressions VALUES ('store','test',?,'review_invitation',100)", ("a" * 64,)
    )
    reviews.db.commit()
    with pytest.raises(ValueError):
        asyncio.run(claim_delivery(reviews, **claim))
    reviews, claim = draft_and_claim()
    assert asyncio.run(claim_delivery(reviews, **claim)) == {"state": "dispatching"}
    reviews.db.execute(
        "INSERT INTO review_recipient_suppressions VALUES ('store','test',?,'review_invitation',102)", ("a" * 64,)
    )
    reviews.db.commit()
    with pytest.raises(ValueError):
        asyncio.run(record_delivery_outcome(reviews, **(claim | {"now": 103}), outcome="accepted", accepted_at=102))
    assert reviews.db.execute("SELECT state FROM invitation_generations").fetchone()[0] != "accepted"
