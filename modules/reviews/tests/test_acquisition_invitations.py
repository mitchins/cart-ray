"""Synthetic acquisition issuance against the real local commerce and Reviews schemas."""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cartray_reviews.invitations import issue_from_settlement, record_signed_consent
from test_acquisition import Database as CommerceDatabase
from test_native_lifecycle import Database as ReviewsDatabase
from test_native_lifecycle import invoke
from test_native_lifecycle import lifecycle as lifecycle


def issue(reviews, commerce, **kwargs):
    params = {
        "store": "store",
        "environment": "test",
        "event_id": "event_1",
        "subject_maps": {"v1": {"product_1": "policy"}},
        "recipient_context_hash": "a" * 64,
        "token_key": b"k" * 32,
        "now": 100,
    }
    return asyncio.run(issue_from_settlement(reviews, commerce, **(params | kwargs)))


def stores():
    commerce = CommerceDatabase()
    commerce.add_purchase()
    reviews = ReviewsDatabase()
    reviews.db.create_function("unixepoch", 0, lambda: 100)
    reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test','policy','product','/policy/')")
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,?,?,?,NULL)",
        ("store", "test", "order_1", "a" * 64, "review_invitation", "synthetic-consent-1", 90),
    )
    reviews.db.commit()
    return reviews, commerce


def test_missing_mismatched_or_revoked_consent_suppresses_invitation():
    for condition in ("missing", "mismatched", "revoked"):
        reviews, commerce = stores()
        if condition == "missing":
            reviews.db.execute("DELETE FROM review_invitation_consents")
        elif condition == "mismatched":
            reviews.db.execute("UPDATE review_invitation_consents SET recipient_context_hash=?", ("b" * 64,))
        else:
            reviews.db.execute("UPDATE review_invitation_consents SET revoked_at=99")
        reviews.db.commit()
        with pytest.raises(ValueError, match="invitation unavailable"):
            issue(reviews, commerce)
        assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitations").fetchone()[0] == 0


def test_authenticated_consent_is_bound_to_confirmed_acquisition_and_recipient():
    reviews, commerce = stores()
    reviews.db.execute("DELETE FROM review_invitation_consents")
    reviews.db.commit()
    data = {
        "event_id": "event_1",
        "recipient_context_hash": "a" * 64,
        "evidence_reference": "proof-1",
        "granted_at": 90,
    }
    secret = b"s" * 32
    signed = json.dumps(
        ["reviews-consent-v1", "store", "test", data], ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    signature = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    args = dict(
        store="store",
        environment="test",
        data=data,
        signature=signature,
        secret=secret,
        subject_maps={"v1": {"product_1": "policy"}},
        now=100,
    )
    with pytest.raises(ValueError):
        asyncio.run(record_signed_consent(reviews, commerce, **(args | {"signature": "0" * 64})))
    assert reviews.db.execute("SELECT COUNT(*) FROM review_invitation_consents").fetchone()[0] == 0
    asyncio.run(record_signed_consent(reviews, commerce, **args))
    asyncio.run(record_signed_consent(reviews, commerce, **args))
    assert issue(reviews, commerce)["subjects"] == ("policy",)
    with pytest.raises(ValueError):
        asyncio.run(record_signed_consent(reviews, commerce, **(args | {"store": "other"})))


def test_signed_consent_route_rejects_forgery_and_records_verified_evidence(lifecycle):
    reviews, commerce = stores()
    reviews.db.execute("DELETE FROM review_invitation_consents")
    reviews.db.commit()
    data = {
        "event_id": "event_1",
        "recipient_context_hash": "a" * 64,
        "evidence_reference": "proof-1",
        "granted_at": 90,
    }
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_CONSENT_SECRET="e" * 40,
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )
    body = json.dumps(
        ["reviews-consent-v1", "store", "test", data], ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    signature = hmac.new(env.REVIEWS_CONSENT_SECRET.encode(), body, hashlib.sha256).hexdigest()

    def post(signature_value):
        request = SimpleNamespace(
            method="POST", headers={"X-Reviews-Consent-Signature": signature_value}, data=data, body=None
        )
        return asyncio.run(lifecycle.handle_write(request, env, "/reviews-internal/v1/consents"))

    with patch.object(lifecycle.time, "time", return_value=101):
        assert post("0" * 64).status == 409
        assert post(signature).status == 201
        assert post(signature).status == 201
    assert reviews.db.execute("SELECT COUNT(*) FROM review_invitation_consents").fetchone()[0] == 1


def test_issue_opaque_scoped_invitation_is_idempotent():
    reviews, commerce = stores()
    first = issue(reviews, commerce)
    assert first == issue(reviews, commerce, now=101)
    assert first["subjects"] == ("policy",)
    stored = reviews.db.execute("SELECT token_hash FROM acquisition_invitations").fetchone()[0]
    assert stored != first["token"]
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitations").fetchone()[0] == 1
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitation_subjects").fetchone()[0] == 1


def test_purchase_submission_accepts_rating_without_a_name_and_rejects_title_only(lifecycle):
    reviews, commerce = stores()
    invitation = issue(reviews, commerce)
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
    )
    path = "/reviews/v1/contributions"
    base = {"subject_id": "policy", "invitation_token": invitation["token"]}
    with patch.object(lifecycle.time, "time", return_value=101):
        assert invoke(lifecycle, env, path, base | {"title": "Nice"}, key="title-only").status == 400
        assert invoke(lifecycle, env, path, base | {"body": "ok"}, key="short-comment").status == 400
        result = invoke(lifecycle, env, path, base | {"rating": 2}, key="rating-only")
        assert result.status == 202
        assert invoke(lifecycle, env, path, base | {"rating": 2}, key="rating-only").body == result.body
    review = reviews.db.execute("SELECT author, rating FROM native_revisions").fetchone()
    assert tuple(review) == ("Anonymous", 2)


def test_invalid_capability_flood_does_not_exhaust_store_allowance(lifecycle):
    reviews, commerce = stores()
    invitation = issue(reviews, commerce)
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
    )
    path = "/reviews/v1/contributions"
    base = {"subject_id": "policy", "rating": 4}
    reviews.db.execute("INSERT INTO native_rate_limits VALUES ('store','test','purchase-submit:old',-3000,1)")
    reviews.db.execute("INSERT INTO native_rate_limits VALUES ('store','test','issue',-3000,1)")
    reviews.db.execute("INSERT INTO acquisition_rate_backstop VALUES ('store','test',-3000,1)")
    reviews.db.execute("INSERT INTO acquisition_rate_maintenance VALUES ('store','test',63)")
    reviews.db.commit()
    with patch.object(lifecycle.time, "time", return_value=101):
        for index in range(40):
            invalid = format(index + 1, "064x")
            assert invoke(lifecycle, env, path, base | {"invitation_token": invalid}, key=f"bad-{index}").status == 409
        assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_rate_backstop").fetchone()[0] == 1
        assert invoke(lifecycle, env, path, base | {"invitation_token": invitation["token"]}, key="valid").status == 202
    assert reviews.db.execute("SELECT count FROM acquisition_rate_backstop").fetchone()[0] == 1
    assert (
        reviews.db.execute("SELECT COUNT(*) FROM native_rate_limits WHERE action='purchase-submit:old'").fetchone()[0]
        == 0
    )
    assert reviews.db.execute("SELECT COUNT(*) FROM native_rate_limits WHERE action='issue'").fetchone()[0] == 1


def test_one_capability_at_limit_does_not_block_another(lifecycle):
    reviews, commerce = stores()
    invitation = issue(reviews, commerce)
    second_token = "f" * 64
    second_hash = hashlib.sha256(second_token.encode()).hexdigest()
    reviews.db.execute(
        "INSERT INTO acquisition_invitations SELECT store_key,environment,'order_2',source_event_id,"
        "recipient_context_hash,subject_set_hash,key_version,?,expires_at,NULL,revocation_epoch,active_key_version "
        "FROM acquisition_invitations",
        (second_hash,),
    )
    reviews.db.execute("INSERT INTO acquisition_invitation_subjects VALUES ('store','test','order_2','policy',NULL)")
    reviews.db.commit()
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
    )
    path = "/reviews/v1/contributions"
    first = {"subject_id": "policy", "rating": 4, "invitation_token": invitation["token"]}
    second = {"subject_id": "policy", "rating": 2, "invitation_token": second_token}
    with patch.object(lifecycle.time, "time", return_value=101):
        for _ in range(30):
            assert invoke(lifecycle, env, path, first, key="first").status == 202
        assert invoke(lifecycle, env, path, first, key="first").status == 429
        assert invoke(lifecycle, env, path, second, key="second").status == 202
    assert reviews.db.execute("SELECT COUNT(*) FROM native_reviews").fetchone()[0] == 2


def test_customer_landing_uses_approved_names_and_scoped_nonconsuming_capability(lifecycle):
    reviews, _ = stores()
    commerce = CommerceDatabase()
    commerce.add_purchase(second=True)
    reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test','second','product','/second/')")
    reviews.db.commit()
    invitation = issue(reviews, commerce, subject_maps={"v1": {"product_1": "policy", "product_2": "second"}})
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_PUBLIC_STORE_NAME="Example Store",
    )
    landing = "/reviews/v1/invitation"
    data = {"invitation_token": invitation["token"]}
    with patch.object(lifecycle.time, "time", return_value=101):
        assert invoke(lifecycle, env, landing, data).status == 404
        name_path = "/reviews-admin/v1/subject-display-names"
        for subject, name in (("policy", "Policy"), ("second", "Second product")):
            assert invoke(lifecycle, env, name_path, {"subject_id": subject, "public_name": name}).status == 401
            assert (
                invoke(lifecycle, env, name_path, {"subject_id": subject, "public_name": name}, admin=True).status
                == 200
            )
        result = invoke(lifecycle, env, landing, data)
        assert result.status == 200
        draft_scope = hmac.new(
            b"d" * 40,
            json.dumps(["reviews-draft-v1", "store", "test", "order_1"], separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert result.body == {
            "store_name": "Example Store",
            "draft_scope": draft_scope,
            "subjects": [
                {"subject_id": "policy", "public_name": "Policy", "progress": "available"},
                {"subject_id": "second", "public_name": "Second product", "progress": "available"},
            ],
        }
        assert invoke(lifecycle, env, landing, {"invitation_token": "0" * 64}).body == {
            "error": "invitation unavailable"
        }
        assert reviews.db.execute("SELECT COUNT(*) FROM native_reviews").fetchone()[0] == 0
        assert (
            invoke(
                lifecycle, env, "/reviews/v1/contributions", data | {"subject_id": "policy", "rating": 5}, key="r1"
            ).status
            == 202
        )
        result = invoke(lifecycle, env, landing, data)
        assert [item["progress"] for item in result.body["subjects"]] == ["submitted", "available"]
        reviews.db.execute("UPDATE acquisition_invitations SET revoked_at=101")
        reviews.db.commit()
        assert invoke(lifecycle, env, landing, data).status == 404


def test_conflict_revocation_and_missing_subject_fail_closed():
    reviews, commerce = stores()
    issue(reviews, commerce)
    with pytest.raises(ValueError):
        issue(reviews, commerce, recipient_context_hash="b" * 64)
    reviews.db.execute("UPDATE acquisition_invitations SET revoked_at=102")
    reviews.db.commit()
    with pytest.raises(ValueError):
        issue(reviews, commerce, now=103)
    reviews, commerce = stores()
    reviews.db.execute("DELETE FROM review_subjects")
    reviews.db.commit()
    with pytest.raises(ValueError):
        issue(reviews, commerce)
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitations").fetchone()[0] == 0


def test_redemption_creates_one_pending_review_and_binds_subject():
    import secrets

    from cartray_reviews.invitations import redeem_purchase

    reviews, commerce = stores()
    invitation = issue(reviews, commerce)
    values = {
        "store": "store",
        "environment": "test",
        "subject": "policy",
        "token": invitation["token"],
        "request_key": "request-1",
        "author": "Guest",
        "title": None,
        "body": "Synthetic review",
        "rating": 5,
        "reviewer_key_secret": b"r" * 32,
        "now": 101,
        "random_token": lambda: secrets.token_hex(32),
    }
    first = asyncio.run(redeem_purchase(reviews, **values))
    assert first["status"] == "pending"
    assert asyncio.run(redeem_purchase(reviews, **values)) == first
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(values | {"request_key": "another"})))
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(values | {"subject": "other"})))
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(values | {"store": "other"})))
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(values | {"environment": "live"})))
    assert reviews.db.execute("SELECT COUNT(*) FROM native_reviews").fetchone()[0] == 1
    assert reviews.db.execute("SELECT COUNT(*) FROM native_review_acquisitions").fetchone()[0] == 1
    assert reviews.db.execute("SELECT COUNT(*) FROM native_audit WHERE action='submitted'").fetchone()[0] == 1


def test_expired_or_revoked_invitation_cannot_create_review():
    import secrets

    from cartray_reviews.invitations import redeem_purchase

    for change in ("expired", "revoked"):
        reviews, commerce = stores()
        invitation = issue(reviews, commerce, lifetime=1)
        if change == "revoked":
            reviews.db.execute("UPDATE acquisition_invitations SET revoked_at=100")
            reviews.db.commit()
        values = {
            "store": "store",
            "environment": "test",
            "subject": "policy",
            "token": invitation["token"],
            "request_key": "request-1",
            "author": "Guest",
            "title": None,
            "body": "Text",
            "rating": 5,
            "reviewer_key_secret": b"r" * 32,
            "now": 101,
            "random_token": lambda: secrets.token_hex(32),
        }
        with pytest.raises(ValueError):
            asyncio.run(redeem_purchase(reviews, **values))
        assert reviews.db.execute("SELECT COUNT(*) FROM native_reviews").fetchone()[0] == 0


def test_database_clock_prevents_expired_redemption_after_request_started():
    import secrets

    from cartray_reviews.invitations import redeem_purchase

    reviews, commerce = stores()
    invitation = issue(reviews, commerce, lifetime=10)
    reviews.db.create_function("unixepoch", 0, lambda: 111)
    with pytest.raises(ValueError):
        asyncio.run(
            redeem_purchase(
                reviews,
                store="store",
                environment="test",
                subject="policy",
                token=invitation["token"],
                request_key="request",
                author="Guest",
                title=None,
                body="Text",
                rating=5,
                reviewer_key_secret=b"r" * 32,
                now=100,
                random_token=lambda: secrets.token_hex(32),
            )
        )
    assert reviews.db.execute("SELECT COUNT(*) FROM native_reviews").fetchone()[0] == 0


def test_settlement_to_pending_approval_and_public_verified_review(lifecycle):
    import json
    from types import SimpleNamespace

    from test_native_lifecycle import invoke

    reviews, commerce = stores()
    reviews.db.execute(
        "INSERT INTO native_publication_versions(store_key,environment,subject_id) VALUES ('store','test','policy')"
    )
    reviews.db.commit()
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="s" * 40,
        REVIEWS_INVITATION_KEY="i" * 40,
        REVIEWS_REVIEWER_KEY="r" * 40,
        REVIEWS_DELIVERY_TOKEN="d" * 40,
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )

    def internal(path, data, token):
        request = SimpleNamespace(method="POST", headers={"Authorization": "Bearer " + token}, data=data, body=None)
        return asyncio.run(lifecycle.handle_write(request, env, path))

    settlement = "/reviews-internal/v1/settlements"
    event = {"event_id": "event_1", "recipient_context_hash": "a" * 64, "request_key": "initial"}
    assert internal(settlement, event, env.REVIEWS_TEST_ADMIN_TOKEN).status == 401
    env.REVIEWS_REVIEWER_KEY = env.REVIEWS_SETTLEMENT_TOKEN
    assert internal(settlement, event, env.REVIEWS_SETTLEMENT_TOKEN).status == 503
    env.REVIEWS_REVIEWER_KEY = "r" * 40
    invitation = internal(settlement, event, env.REVIEWS_SETTLEMENT_TOKEN)
    assert invitation.status == 201
    assert internal(settlement, event, env.REVIEWS_SETTLEMENT_TOKEN).body == invitation.body
    delivery = {
        "acquisition_id": invitation.body["acquisition_id"],
        "generation_id": invitation.body["generation_id"],
        "delivery_identity": invitation.body["delivery_identity"],
        "outcome": "accepted",
        "accepted_at": int(lifecycle.time.time()),
    }
    claim = {key: value for key, value in delivery.items() if key not in ("outcome", "accepted_at")}
    assert internal("/reviews-internal/v1/invitations/delivery-claim", claim, env.REVIEWS_DELIVERY_TOKEN).status == 200
    assert (
        internal("/reviews-internal/v1/invitations/delivery-outcome", delivery, env.REVIEWS_DELIVERY_TOKEN).status
        == 200
    )
    assert invoke(lifecycle, env, "/reviews-admin/v1/test-capabilities", {}, admin=True).status == 404
    body = {
        "subject_id": "policy",
        "author": "Guest",
        "body": "A genuine purchase",
        "rating": 5,
        "invitation_token": invitation.body["token"],
    }
    pending = invoke(lifecycle, env, "/reviews/v1/contributions", body, key="request-1")
    assert pending.status == 202
    assert invoke(lifecycle, env, "/reviews/v1/contributions", body, key="request-1").body == pending.body
    assert invoke(lifecycle, env, "/reviews/v1/contributions", body, key="other").status == 409
    before = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "contributions", {}, 0))
    assert before.body["contributions"] == []
    action = "/reviews-admin/v1/contributions/" + pending.body["review_id"] + "/approve"
    assert invoke(lifecycle, env, action, {"revision_id": pending.body["revision_id"]}, admin=True).status == 200
    public = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "contributions", {}, 1))
    assert public.body["contributions"][0]["verification"] == "cartray_verified_acquisition"
    assert invitation.body["token"] not in json.dumps(public.body)


def test_one_invitation_covers_two_paid_subjects_without_resetting_consumption():
    import secrets

    from cartray_reviews.invitations import redeem_purchase

    commerce = CommerceDatabase()
    commerce.add_purchase(second=True, amount=200)
    reviews = ReviewsDatabase()
    reviews.db.create_function("unixepoch", 0, lambda: 100)
    for subject in ("policy", "other-policy"):
        reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test',?,'product','/')", (subject,))
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,?,?,?,NULL)",
        ("store", "test", "order_1", "a" * 64, "review_invitation", "synthetic-consent-1", 90),
    )
    reviews.db.commit()
    mapping = {"v1": {"product_1": "policy", "product_2": "other-policy"}}
    invitation = issue(reviews, commerce, subject_maps=mapping)
    assert invitation["subjects"] == ("other-policy", "policy")
    common = {
        "store": "store",
        "environment": "test",
        "token": invitation["token"],
        "author": "Guest",
        "title": None,
        "body": "Text",
        "rating": 5,
        "reviewer_key_secret": b"r" * 32,
        "now": 101,
        "random_token": lambda: secrets.token_hex(32),
    }
    first = asyncio.run(redeem_purchase(reviews, **(common | {"subject": "policy", "request_key": "first"})))
    assert first["status"] == "pending"
    assert issue(reviews, commerce, subject_maps=mapping, now=102) == invitation
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(common | {"subject": "policy", "request_key": "duplicate"})))
    second = asyncio.run(redeem_purchase(reviews, **(common | {"subject": "other-policy", "request_key": "second"})))
    assert second["status"] == "pending" and second["review_id"] != first["review_id"]
    assert reviews.db.execute("SELECT COUNT(*) FROM native_review_acquisitions").fetchone()[0] == 2


def test_missing_one_eligible_subject_rolls_back_entire_issuance():
    commerce = CommerceDatabase()
    commerce.add_purchase(second=True, amount=200)
    reviews = ReviewsDatabase()
    reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test','policy','product','/')")
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,?,?,?,NULL)",
        ("store", "test", "order_1", "a" * 64, "review_invitation", "synthetic-consent-1", 90),
    )
    reviews.db.commit()
    mapping = {"v1": {"product_1": "policy", "product_2": "missing"}}
    with pytest.raises(ValueError):
        issue(reviews, commerce, subject_maps=mapping)
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitations").fetchone()[0] == 0
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitation_subjects").fetchone()[0] == 0


def test_replacement_is_pending_until_delivery_and_preserves_consumed_subjects():
    import secrets

    from cartray_reviews.invitations import (
        claim_delivery,
        prepare_replacement,
        record_delivery_outcome,
        redeem_purchase,
    )

    commerce = CommerceDatabase()
    commerce.add_purchase(second=True, amount=200)
    reviews = ReviewsDatabase()
    reviews.db.create_function("unixepoch", 0, lambda: 100)
    for subject in ("policy", "other-policy"):
        reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test',?,'product','/')", (subject,))
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,?,?,?,NULL)",
        ("store", "test", "order_1", "a" * 64, "review_invitation", "consent", 90),
    )
    reviews.db.commit()
    mapping = {"v1": {"product_1": "policy", "product_2": "other-policy"}}
    initial = issue(reviews, commerce, subject_maps=mapping)
    common = dict(
        store="store",
        environment="test",
        subject="policy",
        token=initial["token"],
        request_key="first",
        author="Guest",
        title=None,
        body="Good",
        rating=5,
        reviewer_key_secret=b"r" * 32,
        now=101,
        random_token=lambda: secrets.token_hex(32),
    )
    first_review = asyncio.run(redeem_purchase(reviews, **common))
    assert first_review["status"] == "pending"
    params = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        request_key="replacement-1",
        key_version="v2",
        secret=b"z" * 32,
        now=102,
        random_token=lambda: secrets.token_hex(32),
    )
    replacement = asyncio.run(prepare_replacement(reviews, **params))
    assert asyncio.run(prepare_replacement(reviews, **params)) == replacement
    assert replacement["token"] != initial["token"]
    with pytest.raises(ValueError):
        asyncio.run(
            redeem_purchase(
                reviews,
                **(common | {"token": replacement["token"], "subject": "other-policy", "request_key": "pre-send"}),
            )
        )
    outcome = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        generation=replacement["generation_id"],
        delivery=replacement["delivery_identity"],
        now=200,
    )
    claim = {k: v for k, v in outcome.items() if k != "now"} | {"now": 102}
    assert asyncio.run(claim_delivery(reviews, **claim)) == {"state": "dispatching"}
    with pytest.raises(ValueError):
        asyncio.run(claim_delivery(reviews, **claim))
    assert asyncio.run(record_delivery_outcome(reviews, **outcome, outcome="accepted", accepted_at=103)) == {
        "state": "accepted"
    }
    assert asyncio.run(record_delivery_outcome(reviews, **outcome, outcome="accepted", accepted_at=103)) == {
        "state": "accepted"
    }
    row = reviews.db.execute("SELECT expires_at FROM acquisition_invitations").fetchone()
    assert row[0] == 103 + 60 * 86400
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(common | {"request_key": "old-link"})))
    assert asyncio.run(redeem_purchase(reviews, **(common | {"token": replacement["token"]}))) == first_review
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **(common | {"token": replacement["token"], "request_key": "duplicate"})))
    second = asyncio.run(
        redeem_purchase(
            reviews, **(common | {"token": replacement["token"], "subject": "other-policy", "request_key": "second"})
        )
    )
    assert second["status"] == "pending"
    assert reviews.db.execute("SELECT COUNT(*) FROM native_review_acquisitions").fetchone()[0] == 2


def test_uncertain_delivery_cannot_be_replaced_or_activated_without_resolution():
    import secrets

    from cartray_reviews.invitations import claim_delivery, prepare_replacement, record_delivery_outcome

    reviews, commerce = stores()
    issue(reviews, commerce)
    params = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        request_key="send-1",
        key_version="v2",
        secret=b"z" * 32,
        now=102,
        random_token=lambda: secrets.token_hex(32),
    )
    draft = asyncio.run(prepare_replacement(reviews, **params))
    outcome = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        generation=draft["generation_id"],
        delivery=draft["delivery_identity"],
        now=103,
    )
    assert asyncio.run(claim_delivery(reviews, **(outcome | {"now": 102}))) == {"state": "dispatching"}
    assert asyncio.run(record_delivery_outcome(reviews, **outcome, outcome="uncertain")) == {"state": "uncertain"}
    assert "token" not in asyncio.run(prepare_replacement(reviews, **params))
    with pytest.raises(ValueError):
        asyncio.run(prepare_replacement(reviews, **(params | {"request_key": "send-2"})))
    assert asyncio.run(record_delivery_outcome(reviews, **outcome, outcome="failed")) == {"state": "failed"}
    assert asyncio.run(prepare_replacement(reviews, **(params | {"request_key": "send-2"})))["state"] == "prepared"


def test_replacement_endpoints_separate_issuer_and_delivery_credentials(lifecycle):
    reviews, commerce = stores()
    issue(reviews, commerce)
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_CONSENT_SECRET="e" * 40,
        REVIEWS_DELIVERY_TOKEN="f" * 40,
        REVIEWS_INVITATION_KEY_VERSION="v2",
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )

    def post(path, data, credential):
        request = SimpleNamespace(
            method="POST", headers={"Authorization": "Bearer " + credential}, data=data, body=None
        )
        return asyncio.run(lifecycle.handle_write(request, env, path))

    replacement_path = "/reviews-internal/v1/invitations/replacement"
    outcome_path = "/reviews-internal/v1/invitations/delivery-outcome"
    with patch.object(lifecycle.time, "time", return_value=101):
        assert (
            post(replacement_path, {"event_id": "event_1", "request_key": "one"}, env.REVIEWS_TEST_ADMIN_TOKEN).status
            == 401
        )
        prepared = post(replacement_path, {"event_id": "event_1", "request_key": "one"}, env.REVIEWS_SETTLEMENT_TOKEN)
        assert prepared.status == 201
        payload = {
            "acquisition_id": "order_1",
            "generation_id": prepared.body["generation_id"],
            "delivery_identity": prepared.body["delivery_identity"],
            "outcome": "accepted",
            "accepted_at": 101,
        }
        assert post(outcome_path, payload, env.REVIEWS_SETTLEMENT_TOKEN).status == 401
        claim = {k: v for k, v in payload.items() if k not in ("outcome", "accepted_at")}
        assert post("/reviews-internal/v1/invitations/delivery-claim", claim, env.REVIEWS_DELIVERY_TOKEN).status == 200
        assert post(outcome_path, payload, env.REVIEWS_DELIVERY_TOKEN).body == {"state": "accepted"}


def test_revocation_after_preparation_cannot_be_undone_by_delivery_acknowledgement():
    import secrets

    from cartray_reviews.invitations import (
        claim_delivery,
        prepare_replacement,
        record_delivery_outcome,
        revoke_purchase,
    )

    reviews, commerce = stores()
    initial = issue(reviews, commerce)
    params = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        request_key="pending",
        key_version="v2",
        secret=b"z" * 32,
        now=102,
        random_token=lambda: secrets.token_hex(32),
    )
    draft = asyncio.run(prepare_replacement(reviews, **params))
    asyncio.run(
        claim_delivery(
            reviews,
            store="store",
            environment="test",
            acquisition="order_1",
            generation=draft["generation_id"],
            delivery=draft["delivery_identity"],
            now=102,
        )
    )
    asyncio.run(revoke_purchase(reviews, store="store", environment="test", token=initial["token"], now=103))
    assert asyncio.run(
        record_delivery_outcome(
            reviews,
            store="store",
            environment="test",
            acquisition="order_1",
            generation=draft["generation_id"],
            delivery=draft["delivery_identity"],
            outcome="accepted",
            now=104,
            accepted_at=104,
        )
    ) == {"state": "revoked"}
    row = reviews.db.execute("SELECT token_hash,revoked_at FROM acquisition_invitations").fetchone()
    assert row[1] == 103
    assert row[0] == hashlib.sha256(initial["token"].encode()).hexdigest()
    second = asyncio.run(prepare_replacement(reviews, **(params | {"request_key": "retry", "now": 106})))
    asyncio.run(
        claim_delivery(
            reviews,
            store="store",
            environment="test",
            acquisition="order_1",
            generation=second["generation_id"],
            delivery=second["delivery_identity"],
            now=106,
        )
    )
    asyncio.run(revoke_purchase(reviews, store="store", environment="test", token=initial["token"], now=107))
    assert asyncio.run(
        record_delivery_outcome(
            reviews,
            store="store",
            environment="test",
            acquisition="order_1",
            generation=second["generation_id"],
            delivery=second["delivery_identity"],
            outcome="accepted",
            now=108,
            accepted_at=108,
        )
    ) == {"state": "revoked"}


def test_initial_capability_is_inactive_until_provider_acceptance():
    import secrets

    from cartray_reviews.invitations import (
        claim_delivery,
        prepare_initial_delivery,
        record_delivery_outcome,
        redeem_purchase,
    )

    reviews, commerce = stores()
    params = dict(
        store="store",
        environment="test",
        event_id="event_1",
        subject_maps={"v1": {"product_1": "policy"}},
        recipient_context_hash="a" * 64,
        request_key="initial",
        key_version="v3",
        secret=b"z" * 32,
        now=100,
        random_token=lambda: secrets.token_hex(32),
    )
    draft = asyncio.run(prepare_initial_delivery(reviews, commerce, **params))
    assert draft == asyncio.run(prepare_initial_delivery(reviews, commerce, **params))
    assert reviews.db.execute("SELECT expires_at FROM acquisition_invitations").fetchone()[0] == 0
    attempt = dict(
        store="store",
        environment="test",
        subject="policy",
        token=draft["token"],
        request_key="review",
        author="Guest",
        title=None,
        body="",
        rating=4,
        reviewer_key_secret=b"r" * 32,
        now=101,
        random_token=lambda: secrets.token_hex(32),
    )
    with pytest.raises(ValueError):
        asyncio.run(redeem_purchase(reviews, **attempt))
    outcome = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        generation=draft["generation_id"],
        delivery=draft["delivery_identity"],
        outcome="accepted",
        accepted_at=120,
        now=150,
    )
    assert asyncio.run(
        claim_delivery(
            reviews, **{k: v for k, v in outcome.items() if k not in ("outcome", "accepted_at", "now")}, now=101
        )
    ) == {"state": "dispatching"}
    assert asyncio.run(record_delivery_outcome(reviews, **outcome)) == {"state": "accepted"}
    assert reviews.db.execute("SELECT expires_at,active_key_version FROM acquisition_invitations").fetchone()[:] == (
        120 + 60 * 86400,
        "v3",
    )
    assert asyncio.run(redeem_purchase(reviews, **(attempt | {"now": 151})))["status"] == "pending"


def test_revocation_before_dispatch_closes_preparation_and_allows_recovery():
    import secrets

    from cartray_reviews.invitations import prepare_replacement, revoke_purchase

    reviews, commerce = stores()
    original = issue(reviews, commerce)
    params = dict(
        store="store",
        environment="test",
        acquisition="order_1",
        request_key="first-draft",
        key_version="v1",
        secret=b"z" * 32,
        now=102,
        random_token=lambda: secrets.token_hex(32),
    )
    first = asyncio.run(prepare_replacement(reviews, **params))
    asyncio.run(revoke_purchase(reviews, store="store", environment="test", token=original["token"], now=103))
    assert (
        reviews.db.execute(
            "SELECT state FROM invitation_generations WHERE generation_id=?", (first["generation_id"],)
        ).fetchone()[0]
        == "failed"
    )
    second = asyncio.run(prepare_replacement(reviews, **(params | {"request_key": "second-draft", "now": 104})))
    assert second["state"] == "prepared" and second["token"] != first["token"]


def test_unsent_cancellation_cannot_override_concurrent_dispatch_claim():
    import secrets

    from cartray_reviews.invitations import prepare_replacement, record_delivery_outcome

    reviews, commerce = stores()
    issue(reviews, commerce)
    draft = asyncio.run(
        prepare_replacement(
            reviews,
            store="store",
            environment="test",
            acquisition="order_1",
            request_key="race-cancel",
            key_version="v1",
            secret=b"z" * 32,
            now=102,
            random_token=lambda: secrets.token_hex(32),
        )
    )
    original_batch = reviews.batch

    async def racing_batch(statements):
        reviews.db.execute(
            "UPDATE invitation_generations SET state='dispatching' WHERE generation_id=?", (draft["generation_id"],)
        )
        reviews.db.commit()
        return await original_batch(statements)

    reviews.batch = racing_batch
    with pytest.raises(ValueError):
        asyncio.run(
            record_delivery_outcome(
                reviews,
                store="store",
                environment="test",
                acquisition="order_1",
                generation=draft["generation_id"],
                delivery=draft["delivery_identity"],
                outcome="failed",
                now=103,
            )
        )
    assert reviews.db.execute("SELECT state FROM invitation_generations").fetchone()[0] == "dispatching"


def test_scoped_management_edits_and_withdraws_without_public_leak(lifecycle):
    reviews, commerce = stores()
    reviews.db.execute(
        "INSERT INTO native_publication_versions(store_key,environment,subject_id) VALUES ('store','test','policy')"
    )
    reviews.db.commit()
    invitation = issue(reviews, commerce)
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
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
        pending = invoke(
            lifecycle,
            env,
            "/reviews/v1/contributions",
            {
                "subject_id": "policy",
                "body": "Original private body",
                "rating": 5,
                "invitation_token": invitation["token"],
            },
            key="submission",
        )
        assert pending.status == 202
        review = pending.body["review_id"]
        token = pending.body["management_token"]
        revision = pending.body["revision_id"]
        approval = "/reviews-admin/v1/contributions/" + review + "/approve"
        assert invoke(lifecycle, env, approval, {"revision_id": revision}, admin=True).status == 200
        manage = f"/reviews/v1/manage/{review}/"
        assert invoke(lifecycle, env, manage + "read", {"management_token": "0" * 64}).status == 409
        assert (
            invoke(lifecycle, env, manage + "read", {"management_token": token}).body["body"] == "Original private body"
        )
        payload = {
            "management_token": token,
            "revision_id": revision,
            "update": {"body": "Corrected body", "rating": 2},
        }
        edited = invoke(lifecycle, env, manage + "edit", payload, key="edit-one")
        assert edited.status == 200 and edited.body["status"] == "pending"
        assert invoke(lifecycle, env, manage + "edit", payload, key="edit-one").body == edited.body
        summary = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "summary", {}, 2))
        assert summary.body["rating"] == {"average": None, "rated_count": 0}
        listing = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "contributions", {}, 2))
        assert listing.body["contributions"] == []
        new_revision = edited.body["revision_id"]
        assert new_revision != revision
        assert invoke(lifecycle, env, approval, {"revision_id": new_revision}, admin=True).status == 200
        summary = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "summary", {}, 3))
        assert summary.body["rating"] == {"average": 2, "rated_count": 1}
        withdraw = {"management_token": token, "revision_id": new_revision}
        assert invoke(lifecycle, env, manage + "withdraw", withdraw, key="withdraw-one").body["status"] == "withdrawn"
        assert invoke(lifecycle, env, manage + "withdraw", withdraw, key="withdraw-one").body["status"] == "withdrawn"
        assert invoke(lifecycle, env, manage + "read", {"management_token": token}).status == 409
        summary = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "summary", {}, 4))
        assert summary.body["rating"] == {"average": None, "rated_count": 0}
        public = asyncio.run(lifecycle.native_read(reviews, "store", "test", "policy", "contributions", {}, 4))
        assert public.body["contributions"] == []
        assert reviews.db.execute("SELECT COUNT(*) FROM native_revisions").fetchone()[0] == 2
        assert (
            reviews.db.execute("SELECT COUNT(*) FROM native_audit WHERE actor='reviewer_capability'").fetchone()[0] == 2
        )
        used = reviews.db.execute(
            "SELECT count FROM native_rate_limits WHERE action LIKE 'review-manage:%'"
        ).fetchone()[0]
        for _ in range(30 - used):
            assert invoke(lifecycle, env, manage + "read", {"management_token": token}).status == 409
        assert invoke(lifecycle, env, manage + "read", {"management_token": token}).status == 429


def test_edge_admission_rejects_before_d1_and_fails_closed(lifecycle):
    class NoD1:
        def prepare(self, _sql):
            raise AssertionError("D1 must not be reached")

    async def deny(_options):
        return SimpleNamespace(success=False)

    env = SimpleNamespace(
        REVIEWS_DB=NoD1(),
        CARTRAY_DB=object(),
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_INGRESS=SimpleNamespace(limit=deny),
    )
    request = {"subject_id": "policy", "rating": 2, "invitation_token": "0" * 64}
    assert invoke(lifecycle, env, "/reviews/v1/contributions", request, key="attempt").status == 429
    env.REVIEWS_INGRESS = None
    assert invoke(lifecycle, env, "/reviews/v1/contributions", request, key="attempt").status == 503

    async def broken(_options):
        raise RuntimeError("provider unavailable")

    env.REVIEWS_INGRESS = SimpleNamespace(limit=broken)
    assert invoke(lifecycle, env, "/reviews/v1/contributions", request, key="attempt").status == 503
    observed = []

    async def capture(options):
        observed.append(options["key"])
        return SimpleNamespace(success=False)

    env.REVIEWS_INGRESS = SimpleNamespace(limit=capture)
    for ip in ("192.0.2.1", "192.0.2.2"):
        obj = SimpleNamespace(method="POST", headers={"CF-Connecting-IP": ip}, data=request, body=None)
        assert asyncio.run(lifecycle.handle_write(obj, env, "/reviews/v1/contributions")).status == 429
    assert observed[0] != observed[1]
    assert all("192.0.2." not in key for key in observed)


def test_recovery_request_is_generic_bounded_and_does_not_accept_an_address(lifecycle):
    from cartray_reviews.invitations import revoke_purchase

    reviews, commerce = stores()
    original = issue(reviews, commerce)
    asyncio.run(revoke_purchase(reviews, store="store", environment="test", token=original["token"], now=100))
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_DELIVERY_TOKEN="f" * 40,
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )
    path = "/reviews/v1/invitation-recovery"
    with patch.object(lifecycle.time, "time", return_value=101):
        unknown = invoke(lifecycle, env, path, {"invitation_token": "0" * 64})
        genuine = invoke(lifecycle, env, path, {"invitation_token": original["token"]})
        assert unknown.status == genuine.status == 202 and unknown.body == genuine.body == {"status": "requested"}
        assert (
            invoke(lifecycle, env, path, {"invitation_token": original["token"], "email": "other@example.test"}).status
            == 400
        )
        assert invoke(lifecycle, env, path, {"invitation_token": original["token"]}).status == 202
        assert reviews.db.execute("SELECT COUNT(*) FROM review_recovery_requests").fetchone()[0] == 1
        request = SimpleNamespace(
            method="POST", headers={"Authorization": "Bearer " + env.REVIEWS_SETTLEMENT_TOKEN}, data={}, body=None
        )
        pending = asyncio.run(lifecycle.handle_write(request, env, "/reviews-internal/v1/invitations/recovery-next"))
        assert pending.status == 200 and pending.body["pending"]["acquisition_id"] == "order_1"
        assert original["token"] not in json.dumps(pending.body)
        recovery_id = pending.body["pending"]["request_id"]
        prepare_data = {"event_id": "event_1", "request_id": recovery_id}
        request.data = prepare_data
        prepared = asyncio.run(
            lifecycle.handle_write(request, env, "/reviews-internal/v1/invitations/recovery-prepare")
        )
        assert prepared.status == 201
        assert (
            asyncio.run(lifecycle.handle_write(request, env, "/reviews-internal/v1/invitations/recovery-prepare")).body
            == prepared.body
        )
        claim = {
            "acquisition_id": "order_1",
            "generation_id": prepared.body["generation_id"],
            "delivery_identity": prepared.body["delivery_identity"],
        }
        delivery_request = SimpleNamespace(
            method="POST", headers={"Authorization": "Bearer " + env.REVIEWS_DELIVERY_TOKEN}, data=claim, body=None
        )
        assert (
            asyncio.run(
                lifecycle.handle_write(delivery_request, env, "/reviews-internal/v1/invitations/delivery-claim")
            ).status
            == 200
        )
        delivery_request.data = claim | {"outcome": "accepted", "accepted_at": 101}
        assert (
            asyncio.run(
                lifecycle.handle_write(delivery_request, env, "/reviews-internal/v1/invitations/delivery-outcome")
            ).status
            == 200
        )
        assert reviews.db.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "fulfilled"
        assert (
            asyncio.run(
                lifecycle.handle_write(request, env, "/reviews-internal/v1/invitations/recovery-prepare")
            ).status
            == 409
        )


def test_recovery_queue_skips_revoked_consent_and_claimed_deliveries(lifecycle):
    import secrets

    from cartray_reviews.invitations import prepare_replacement

    reviews, commerce = stores()
    original = issue(reviews, commerce)
    reviews.db.execute(
        "INSERT INTO acquisition_invitations (store_key,environment,acquisition_id,source_event_id,"
        "recipient_context_hash,subject_set_hash,key_version,token_hash,expires_at) "
        "SELECT store_key,environment,'order_2',source_event_id,recipient_context_hash,subject_set_hash, "
        "key_version,?,expires_at "
        "FROM acquisition_invitations",
        ("e" * 64,),
    )
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,?,?,?,NULL)",
        ("store", "test", "order_2", "a" * 64, "review_invitation", "consent-2", 90),
    )
    reviews.db.execute("UPDATE review_invitation_consents SET revoked_at=101 WHERE acquisition_id='order_1'")
    reviews.db.execute(
        "INSERT INTO review_recovery_requests VALUES ('store','test','order_1',?,1,'pending')", ("1" * 64,)
    )
    reviews.db.execute(
        "INSERT INTO review_recovery_requests VALUES ('store','test','order_2',?,2,'pending')", ("2" * 64,)
    )
    reviews.db.commit()
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
    )
    req = SimpleNamespace(
        method="POST", headers={"Authorization": "Bearer " + env.REVIEWS_SETTLEMENT_TOKEN}, data={}, body=None
    )
    result = asyncio.run(lifecycle.handle_write(req, env, "/reviews-internal/v1/invitations/recovery-next"))
    assert result.status == 200 and result.body["pending"]["acquisition_id"] == "order_2"
    assert original["token"] not in json.dumps(result.body)
    reviews.db.execute(
        "INSERT INTO acquisition_invitations (store_key,environment,acquisition_id,source_event_id,"
        "recipient_context_hash,subject_set_hash,key_version,token_hash,expires_at) "
        "SELECT store_key,environment,'order_3',source_event_id,recipient_context_hash,subject_set_hash, "
        "key_version,?,expires_at FROM acquisition_invitations WHERE acquisition_id='order_2'",
        ("f" * 64,),
    )
    reviews.db.execute(
        "INSERT INTO review_invitation_consents VALUES (?,?,?,?,?,?,?,NULL)",
        ("store", "test", "order_3", "a" * 64, "review_invitation", "consent-3", 90),
    )
    reviews.db.execute(
        "INSERT INTO review_recovery_requests VALUES ('store','test','order_3',?,3,'pending')", ("3" * 64,)
    )
    reviews.db.commit()
    req.data = {"request_id": "2" * 64}
    deferred = asyncio.run(lifecycle.handle_write(req, env, "/reviews-internal/v1/invitations/recovery-defer"))
    assert deferred.status == 200
    req.data = {}
    next_request = asyncio.run(lifecycle.handle_write(req, env, "/reviews-internal/v1/invitations/recovery-next"))
    assert next_request.body["pending"]["acquisition_id"] == "order_3"
    reviews.db.execute("DELETE FROM review_recovery_deferrals")
    reviews.db.commit()
    asyncio.run(
        prepare_replacement(
            reviews,
            store="store",
            environment="test",
            acquisition="order_2",
            request_key="manual-pending",
            key_version="v1",
            secret=b"c" * 40,
            now=102,
            random_token=lambda: secrets.token_hex(32),
        )
    )
    result = asyncio.run(lifecycle.handle_write(req, env, "/reviews-internal/v1/invitations/recovery-next"))
    assert result.status == 200 and result.body["pending"]["acquisition_id"] == "order_3"


def test_queued_recovery_is_durable_before_d1_and_selects_canonical_request(lifecycle):
    from cartray_reviews.invitations import revoke_purchase

    reviews, commerce = stores()
    original = issue(reviews, commerce)
    asyncio.run(revoke_purchase(reviews, store="store", environment="test", token=original["token"], now=100))
    messages = []

    async def enqueue(message):
        messages.append(message)

    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_RECOVERY_AUTOMATIC="1",
        REVIEWS_RECOVERY_QUEUE=SimpleNamespace(send=enqueue),
    )
    with patch.object(lifecycle.time, "time", return_value=101):
        assert (
            invoke(lifecycle, env, "/reviews/v1/invitation-recovery", {"invitation_token": original["token"]}).status
            == 202
        )
        assert (
            invoke(lifecycle, env, "/reviews/v1/invitation-recovery", {"invitation_token": original["token"]}).status
            == 202
        )
        assert reviews.db.execute("SELECT COUNT(*) FROM review_recovery_requests").fetchone()[0] == 0
        assert len(messages) == 2
        assert original["token"] not in json.dumps(messages)

        def command(message):
            request = SimpleNamespace(
                method="POST",
                headers={"Authorization": "Bearer " + env.REVIEWS_SETTLEMENT_TOKEN},
                data=message,
                body=None,
            )
            return asyncio.run(
                lifecycle.handle_write(request, env, "/reviews-internal/v1/invitations/recovery-command")
            )

        first = command(messages[0])
        second = command(messages[1])
        assert first.status == second.status == 200
        assert first.body["pending"]["request_id"] == second.body["pending"]["request_id"] == messages[0]["request_id"]
        assert command(messages[0]).body == first.body
        reviews.db.execute("UPDATE review_recovery_requests SET state='fulfilled'")
        reviews.db.commit()
    with patch.object(lifecycle.time, "time", return_value=101 + 86400):
        assert command(messages[1]).status == 409
        assert reviews.db.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "fulfilled"
    env.REVIEWS_RECOVERY_QUEUE = None
    assert (
        invoke(lifecycle, env, "/reviews/v1/invitation-recovery", {"invitation_token": original["token"]}).status == 503
    )


def test_stale_recovery_command_never_rearms_during_fulfilment():
    from cartray_reviews.recovery import accept_recovery_command

    reviews, commerce = stores()
    original = issue(reviews, commerce)
    reviews.db.execute(
        "INSERT INTO review_recovery_requests VALUES ('store','test','order_1',?,1,'pending')", ("1" * 64,)
    )
    reviews.db.commit()

    async def no_write(_statements):
        raise AssertionError("a stale command must never write")

    reviews.batch = no_write
    result = asyncio.run(
        accept_recovery_command(
            reviews,
            store="store",
            environment="test",
            token_hash=hashlib.sha256(original["token"].encode()).hexdigest(),
            request_id="2" * 64,
            issued_at=1,
            now=86401,
        )
    )
    assert result["request_id"] == "1" * 64
