"""Synthetic acquisition issuance against the real local commerce and Reviews schemas."""

import asyncio

import pytest
from cartray_reviews.invitations import issue_from_settlement
from test_acquisition import Database as CommerceDatabase
from test_native_lifecycle import Database as ReviewsDatabase
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
