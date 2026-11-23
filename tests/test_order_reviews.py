"""Mixed paid orders use native review entitlements and one normal mail path."""

import asyncio
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from kinglet import MockD1Database

from cartray import order_reviews, worker
from cartray.canonical import CanonicalItem
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRequest
from cartray.store import D1OrderStore

ROOT = Path(__file__).parents[1]


def run(value):
    return asyncio.run(value)


@pytest.fixture
def purchase(fixture_catalogue, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "modules/reviews/src"))
    db = MockD1Database()
    for folder in ("migrations", "modules/reviews/migrations", "modules/reviews/consent/migrations"):
        for migration in sorted((ROOT / folder).glob("*.sql")):
            db.conn.executescript(migration.read_text())
    db.conn.execute("INSERT INTO post_purchase_installation VALUES (1,'merchant','test')")
    db.conn.execute("INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','test',0,0)")
    key = "h" * 32
    check = hmac.new(
        key.encode(),
        json.dumps(
            ["reviews-suppression-key-check-v1", "merchant", "test", "installation", "key1"], separators=(",", ":")
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    db.conn.execute(
        "INSERT INTO review_suppression_key_registry VALUES ('merchant','test','installation','key1',?)", (check,)
    )
    db.conn.execute("INSERT INTO review_suppression_key_current VALUES ('merchant','test','installation','key1')")
    for subject in ("first", "second"):
        db.conn.execute(
            "INSERT INTO review_subjects VALUES ('merchant','test',?,'product',?)", (subject, "/" + subject + "/")
        )
        db.conn.execute(
            "INSERT INTO review_subject_display_names VALUES ('merchant','test',?,?)", (subject, subject.title())
        )
    db.conn.commit()
    gateway, store = FakePaymentGateway(), D1OrderStore(db)
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    redirect = run(
        checkout.checkout(
            CheckoutRequest(
                "mixed-order",
                fixture_catalogue.version,
                (CanonicalItem("TEST-BUNDLE", 1), CanonicalItem("TEST-SUPPORT-HOURS", 2)),
            )
        )
    )
    order = gateway.requests[0].order_id
    db.conn.execute(
        "INSERT INTO review_invitation_checkout_choices VALUES (?,1,1,'v1','One review invitation',90)", (order,)
    )
    db.conn.execute(
        "INSERT INTO review_invitation_checkout_grants "
        "VALUES (?,'v1','One review invitation',90,1,'preselected_acceptance')",
        (order,),
    )
    db.conn.commit()
    assert (
        run(
            store.confirm_settlement(
                order_id=order,
                session_id=redirect.session_id,
                event_id="evt_order",
                payload={"id": "evt_order"},
                payload_sha256="sha256:test",
                payment_status="paid",
                amount_total_minor=100,
                now=100,
            )
        )
        is False
    )
    sent = []

    class Mail:
        async def fetch(self, request):
            value = json.loads(request.body)
            if value["type"] != "recipient_evidence":
                sent.append(value)

            async def result():
                return (
                    {"email": "buyer@example.test"}
                    if value["type"] == "recipient_evidence"
                    else {"provider_reference": "accepted"}
                )

            return SimpleNamespace(status=200, json=result)

    env = SimpleNamespace(
        DB=db,
        CARTRAY_ENVIRONMENT="test",
        CARTRAY_POST_PURCHASE_MODE="test",
        CARTRAY_POST_PURCHASE_TOKEN="t" * 32,
        CARTRAY_MERCHANT_ID="merchant",
        CARTRAY_STOREFRONT_ORIGIN="https://shop.example",
        POST_PURCHASE_HANDLER=Mail(),
        CARTRAY_RECIPIENT_HASH_KEY=key,
        CARTRAY_INSTALLATION_ID="installation",
        CARTRAY_REVIEW_SETTINGS=json.dumps(
            {"timing": "both", "selection": "priority", "delay_days": 0, "priorities": ["second"]}
        ),
        CARTRAY_REVIEW_SUBJECT_MAPS=json.dumps(
            {fixture_catalogue.version: {"TEST-BUNDLE": "first", "TEST-SUPPORT-HOURS": "second"}}
        ),
    )
    monkeypatch.setattr(worker, "_internal_post", lambda _url, _token, body: SimpleNamespace(body=body))
    yield db, env, sent, order
    db.conn.close()


def test_receipt_and_followup_share_all_entitlements_but_select_outstanding(purchase):
    db, env, sent, order = purchase
    # Delay the follow-up until a real native subject is consumed.
    env.CARTRAY_REVIEW_SETTINGS = env.CARTRAY_REVIEW_SETTINGS.replace('"delay_days": 0', '"delay_days": 7')
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and sent[0]["type"] == "order_confirmation"
    assert sent[0]["reviews"]["subjects"] == ["second"]
    assert db.conn.execute("SELECT count(*) FROM acquisition_invitation_subjects").fetchone()[0] == 2
    token = db.conn.execute("SELECT invitation_token FROM order_review_messages").fetchone()[0]
    assert tuple(db.conn.execute("SELECT token_hash,expires_at FROM acquisition_invitations").fetchone()) == (
        hashlib.sha256(token.encode()).hexdigest(),
        -1,
    )
    from cartray_reviews.invitations import redeem_purchase

    result = run(
        redeem_purchase(
            db,
            store="merchant",
            environment="test",
            subject="second",
            token=token,
            request_key="purchase-second-review",
            author="Test buyer",
            title="",
            body="Useful product",
            rating=5,
            reviewer_key_secret=b"r" * 32,
            now=200,
            random_token=lambda: secrets.token_hex(32),
        )
    )
    assert result["status"] == "pending"
    assert db.conn.execute("SELECT count(*) FROM native_reviews").fetchone()[0] == 1
    db.conn.execute("UPDATE order_review_messages SET due_at=0")
    db.conn.commit()
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert [message["type"] for message in sent] == ["order_confirmation", "review_followup"]
    assert sent[1]["reviews"]["subjects"] == ["first"]
    assert sent[0]["reviews"]["url"].split("&")[0] == sent[1]["reviews"]["url"].split("&")[0]


@pytest.mark.parametrize(
    "timing,expected",
    [
        ("off", []),
        ("receipt", ["order_confirmation"]),
        ("followup", ["review_followup"]),
        ("both", ["order_confirmation", "review_followup"]),
    ],
)
def test_settings_and_optout_never_suppress_receipt(purchase, timing, expected):
    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": timing, "selection": "all", "delay_days": 0})
    run(worker._drain_post_purchase(env))
    assert len([m for m in sent if m["type"] == "order_confirmation"]) == 1
    assert [m["type"] for m in sent if m.get("reviews")] == expected
    if timing != "off":
        row = run(order_reviews.first(db, "SELECT * FROM order_review_messages"))
        run(order_reviews.unsubscribe(db, "merchant", "test", row["unsubscribe_token"], 200))
        assert not run(order_reviews.permitted(db, "merchant", "test", order, row["recipient_hash"]))


def test_reviews_outage_does_not_prevent_confirmation(purchase):
    db, env, sent, _ = purchase
    env.CARTRAY_RECIPIENT_HASH_KEY = "wrong" * 8
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and sent[0]["type"] == "order_confirmation" and not sent[0].get("reviews")
    assert db.conn.execute("SELECT count(*) FROM acquisition_invitations").fetchone()[0] == 0


def test_existing_capability_is_not_replaced_or_expanded(purchase):
    db, env, sent, order = purchase
    old = "e" * 64
    db.conn.execute(
        "INSERT INTO acquisition_invitations "
        "(store_key,environment,acquisition_id,source_event_id,recipient_context_hash,"
        "subject_set_hash,token_hash,expires_at,key_version) "
        "VALUES ('merchant','test',?,'evt_old',?,?,?,-1,'v1')",
        (order, "f" * 64, "d" * 64, hashlib.sha256(old.encode()).hexdigest()),
    )
    db.conn.execute("INSERT INTO acquisition_invitation_subjects VALUES ('merchant','test',?,'first',NULL)", (order,))
    db.conn.commit()
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and not sent[0].get("reviews")
    assert db.conn.execute("SELECT count(*) FROM order_review_messages").fetchone()[0] == 0
    assert db.conn.execute("SELECT subject_id FROM acquisition_invitation_subjects").fetchone()[0] == "first"


def test_suppression_between_selection_and_claim_blocks_followup(purchase):
    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "both", "delay_days": 7})
    run(worker._drain_post_purchase(env))
    row = run(order_reviews.first(db, "SELECT * FROM order_review_messages"))
    assert run(worker._review_message_links(env, row))["subjects"]
    run(order_reviews.unsubscribe(db, "merchant", "test", row["unsubscribe_token"], 200))
    assert not run(order_reviews.claim_followup(db, row))
    assert db.conn.execute("SELECT state FROM order_review_messages").fetchone()[0] == "skipped"


def test_ambiguous_followup_is_not_resent(purchase):
    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "both", "delay_days": 7})
    run(worker._drain_post_purchase(env))
    original = env.POST_PURCHASE_HANDLER.fetch

    async def timeout_after_acceptance(request):
        result = await original(request)
        if json.loads(request.body)["type"] == "review_followup":
            raise TimeoutError("provider reply lost")
        return result

    env.POST_PURCHASE_HANDLER.fetch = timeout_after_acceptance
    db.conn.execute("UPDATE order_review_messages SET due_at=0")
    db.conn.commit()
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 2
    assert db.conn.execute("SELECT state FROM order_review_messages").fetchone()[0] == "uncertain"


def test_revoked_grant_and_activation_fence_block_new_links(purchase):
    db, env, sent, order = purchase
    db.conn.execute(
        "UPDATE post_purchase_checkpoint SET activation_watermark=(SELECT MAX(id) FROM outbox),"
        "last_outbox_id=(SELECT MAX(id) FROM outbox)"
    )
    db.conn.commit()
    envelope = {
        "type": "recipient_evidence",
        "merchant": "merchant",
        "order_id": order,
        "environment": "test",
        "items": [],
        "access_url": None,
        "session_id": "irrelevant",
    }
    assert run(worker._prepare_order_reviews(env, envelope)) is None
    assert not sent


def test_customer_recovery_uses_the_receipt_provider_and_preserves_progress(purchase):
    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({'timing':'receipt'})
    env.CARTRAY_REVIEW_INVITATION_KEY = 'i'*32
    run(worker._drain_post_purchase(env))
    original = run(order_reviews.first(db, 'SELECT * FROM order_review_messages'))
    db.conn.execute("UPDATE acquisition_invitation_subjects SET consumed_at=100 WHERE subject_id='first'")
    db.conn.execute("INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')", (order,'b'*64))
    db.conn.commit()
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 2
    assert sent[1]['reviews']['subjects'] == ['second']
    assert original['invitation_token'] not in sent[1]['reviews']['url']
    current = run(order_reviews.first(db, 'SELECT * FROM acquisition_invitations'))
    replacement = run(order_reviews.first(db, 'SELECT * FROM order_review_messages'))
    assert current['token_hash'] == hashlib.sha256(replacement['invitation_token'].encode()).hexdigest()
    assert db.conn.execute('SELECT state FROM review_recovery_requests').fetchone()[0] == 'fulfilled'
    assert db.conn.execute(
        "SELECT consumed_at FROM acquisition_invitation_subjects WHERE subject_id='first'").fetchone()[0] == 100
