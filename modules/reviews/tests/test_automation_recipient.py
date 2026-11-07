"""Synthetic recipient binding and existing purpose-specific consent against real SQL."""

import asyncio
import hashlib
import hmac
import json

import pytest
from cartray_reviews.automation_recipient import evidence_status, record_recipient
from cartray_reviews.invitations import record_signed_consent
from test_automation_intake import run, setup

RECIPIENT_SECRET = b"recipient-synthetic-secret-0123456789"
CONSENT_SECRET = b"consent-synthetic-secret-01234567890"
RECIPIENT = hashlib.sha256(b"customer@example.test").hexdigest()


def signed(label, store, env, data, secret):
    raw = json.dumps([label, store, env, data], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()


def bind(reviews, *, data=None, store="store", environment="test", signature=None):
    data = data or {
        "outbox_id": 10,
        "recipient_context_hash": RECIPIENT,
        "binding_version": "v1",
        "evidence_reference": "synthetic-recipient",
        "verified_at": 100,
    }
    signature = signature or signed("reviews-automation-recipient-v1", store, environment, data, RECIPIENT_SECRET)
    return asyncio.run(
        record_recipient(
            reviews,
            store=store,
            environment=environment,
            data=data,
            signature=signature,
            secret=RECIPIENT_SECRET,
            now=120,
        )
    )


def status(reviews, *, store="store", environment="test", outbox_id=10, now=120):
    return asyncio.run(evidence_status(reviews, store=store, environment=environment, outbox_id=outbox_id, now=now))


def consent(checkout, reviews, recipient=RECIPIENT):
    data = {
        "event_id": "event_1",
        "recipient_context_hash": recipient,
        "evidence_reference": "synthetic-consent",
        "granted_at": 100,
    }
    sig = signed("reviews-consent-v1", "store", "test", data, CONSENT_SECRET)
    return asyncio.run(
        record_signed_consent(
            reviews,
            checkout,
            store="store",
            environment="test",
            data=data,
            signature=sig,
            secret=CONSENT_SECRET,
            subject_maps={"v1": {"product_1": "policy"}},
            now=120,
        )
    )


def test_binding_and_matching_consent_are_independent_and_intent_stays_held():
    checkout, reviews = setup()
    run(checkout, reviews)
    assert status(reviews) == {
        "state": "held_eligibility_recipient_consent",
        "recipient_binding_recorded": False,
        "consent_granted": False,
    }
    bind(reviews)
    bind(reviews)
    assert status(reviews)["recipient_binding_recorded"] is True
    assert status(reviews)["consent_granted"] is False
    consent(checkout, reviews)
    result = status(reviews)
    assert result == {
        "state": "held_eligibility_recipient_consent",
        "recipient_binding_recorded": True,
        "consent_granted": True,
    }
    assert RECIPIENT not in str(result)
    assert reviews.db.execute("SELECT count(*) FROM review_automation_recipient_bindings").fetchone()[0] == 1
    assert reviews.db.execute("SELECT count(*) FROM invitation_generations").fetchone()[0] == 0
    assert checkout.db.execute("SELECT delivered_at FROM outbox WHERE id=10").fetchone()[0] is None
    reviews.db.execute("UPDATE review_invitation_consents SET revoked_at=115")
    reviews.db.commit()
    assert status(reviews)["consent_granted"] is False


def test_mismatched_recipient_consent_never_matches():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(reviews)
    consent(checkout, reviews, recipient="b" * 64)
    assert status(reviews)["recipient_binding_recorded"] is True
    assert status(reviews)["consent_granted"] is False


def test_forged_cross_scope_or_changed_binding_fails_without_writes():
    checkout, reviews = setup()
    run(checkout, reviews)
    with pytest.raises(ValueError):
        bind(reviews, signature="0" * 64)
    data = {
        "outbox_id": 10,
        "recipient_context_hash": RECIPIENT,
        "binding_version": "v1",
        "evidence_reference": "synthetic-recipient",
        "verified_at": 100,
    }
    signature = signed("reviews-automation-recipient-v1", "store", "test", data, RECIPIENT_SECRET)
    with pytest.raises(ValueError):
        bind(reviews, data=data, store="other", signature=signature)
    with pytest.raises(ValueError):
        bind(reviews, environment="live")
    assert reviews.db.execute("SELECT count(*) FROM review_automation_recipient_bindings").fetchone()[0] == 0
    bind(reviews, data=data)
    with pytest.raises(ValueError, match="conflict"):
        bind(reviews, data=data | {"recipient_context_hash": "b" * 64})
    assert reviews.db.execute("SELECT count(*) FROM review_automation_recipient_bindings").fetchone()[0] == 1


def test_pre_activation_and_missing_intents_cannot_gain_evidence():
    checkout, reviews = setup(activation=100)
    assert run(checkout, reviews)["status"] == "pre_activation"
    with pytest.raises(ValueError):
        bind(reviews)
    with pytest.raises(ValueError):
        status(reviews)
    assert reviews.db.execute("SELECT count(*) FROM review_automation_recipient_bindings").fetchone()[0] == 0


def withdraw(checkout, reviews, *, recipient=RECIPIENT, overrides=None, signature=None):
    from cartray_reviews.invitations import withdraw_signed_consent

    data = {
        "event_id": "event_1",
        "recipient_context_hash": recipient,
        "evidence_reference": "synthetic-withdrawal",
        "withdrawn_at": 110,
    } | (overrides or {})
    signature = signature or signed("reviews-consent-withdrawal-v1", "store", "test", data, CONSENT_SECRET)
    return asyncio.run(
        withdraw_signed_consent(
            reviews,
            checkout,
            store="store",
            environment="test",
            data=data,
            signature=signature,
            secret=CONSENT_SECRET,
            subject_maps={"v1": {"product_1": "policy"}},
            now=120,
        )
    )


def test_withdrawal_suppresses_existing_consent_without_changing_checkout():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(reviews)
    consent(checkout, reviews)
    assert status(reviews)["consent_granted"] is True
    with pytest.raises(ValueError):
        withdraw(checkout, reviews, signature="0" * 64)
    assert status(reviews)["consent_granted"] is True
    withdraw(checkout, reviews)
    withdraw(checkout, reviews)
    assert status(reviews)["consent_granted"] is False
    assert reviews.db.execute("SELECT revoked_at FROM review_invitation_consents").fetchone()[0] == 110
    assert reviews.db.execute("SELECT COUNT(*) FROM review_invitation_consent_withdrawals").fetchone()[0] == 1
    assert checkout.db.execute("SELECT delivered_at FROM outbox WHERE id=10").fetchone()[0] is None
    with pytest.raises(ValueError):
        consent(checkout, reviews)
    with pytest.raises(ValueError):
        withdraw(checkout, reviews, overrides={"evidence_reference": "conflict"})


def test_withdrawal_before_grant_prevents_out_of_order_resurrection():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(reviews)
    withdraw(checkout, reviews)
    with pytest.raises(ValueError):
        consent(checkout, reviews)
    assert reviews.db.execute("SELECT count(*) FROM review_invitation_consents").fetchone()[0] == 0
    assert status(reviews)["consent_granted"] is False
    with pytest.raises(Exception):
        reviews.db.execute("DELETE FROM review_invitation_consent_withdrawals")
    reviews.db.rollback()


def test_withdrawal_is_bound_to_acquisition_recipient_and_time():
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(reviews)
    consent(checkout, reviews)
    for overrides in ({"event_id": "event_unknown"}, {"withdrawn_at": 121}, {"withdrawn_at": True}):
        with pytest.raises(ValueError):
            withdraw(checkout, reviews, overrides=overrides)
    withdraw(checkout, reviews, recipient="b" * 64)
    assert status(reviews)["consent_granted"] is True
    assert reviews.db.execute("SELECT revoked_at FROM review_invitation_consents").fetchone()[0] is None
