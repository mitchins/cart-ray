"""Synthetic acquisition issuance against the real local commerce and Reviews schemas."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cartray_reviews.invitations import issue_from_settlement
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
    reviews.db.commit()
    return reviews, commerce


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
        assert result.body == {
            "store_name": "Example Store",
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
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )

    def internal(path, data, token):
        request = SimpleNamespace(method="POST", headers={"Authorization": "Bearer " + token}, data=data, body=None)
        return asyncio.run(lifecycle.handle_write(request, env, path))

    settlement = "/reviews-internal/v1/settlements"
    event = {"event_id": "event_1", "recipient_context_hash": "a" * 64}
    assert internal(settlement, event, env.REVIEWS_TEST_ADMIN_TOKEN).status == 401
    env.REVIEWS_REVIEWER_KEY = env.REVIEWS_SETTLEMENT_TOKEN
    assert internal(settlement, event, env.REVIEWS_SETTLEMENT_TOKEN).status == 503
    env.REVIEWS_REVIEWER_KEY = "r" * 40
    invitation = internal(settlement, event, env.REVIEWS_SETTLEMENT_TOKEN)
    assert invitation.status == 201
    assert internal(settlement, event, env.REVIEWS_SETTLEMENT_TOKEN).body == invitation.body
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
    reviews.db.commit()
    mapping = {"v1": {"product_1": "policy", "product_2": "missing"}}
    with pytest.raises(ValueError):
        issue(reviews, commerce, subject_maps=mapping)
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitations").fetchone()[0] == 0
    assert reviews.db.execute("SELECT COUNT(*) FROM acquisition_invitation_subjects").fetchone()[0] == 0
