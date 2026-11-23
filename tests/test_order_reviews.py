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
def purchase(fixture_catalogue, monkeypatch, request):
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
    from cartray.store import review_invitation_disclosure

    version, wording = review_invitation_disclosure(1 if getattr(request, "param", None) == "v1" else 2)
    db.conn.execute("INSERT INTO review_invitation_checkout_choices VALUES (?,1,1,?,?,90)", (order, version, wording))
    db.conn.execute(
        "INSERT INTO review_invitation_checkout_grants VALUES (?,?,?,90,1,'preselected_acceptance')",
        (order, version, wording),
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
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "receipt"})
    env.CARTRAY_REVIEW_INVITATION_KEY = "i" * 32
    run(worker._drain_post_purchase(env))
    original = run(order_reviews.first(db, "SELECT * FROM order_review_messages"))
    db.conn.execute("UPDATE acquisition_invitation_subjects SET consumed_at=100 WHERE subject_id='first'")
    db.conn.execute(
        "INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')", (order, "b" * 64)
    )
    db.conn.commit()
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 2
    assert sent[1]["reviews"]["subjects"] == ["second"]
    assert original["invitation_token"] not in sent[1]["reviews"]["url"]
    current = run(order_reviews.first(db, "SELECT * FROM acquisition_invitations"))
    replacement = run(order_reviews.first(db, "SELECT * FROM order_review_messages"))
    assert current["token_hash"] == hashlib.sha256(replacement["invitation_token"].encode()).hexdigest()
    assert db.conn.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "fulfilled"
    assert (
        db.conn.execute("SELECT consumed_at FROM acquisition_invitation_subjects WHERE subject_id='first'").fetchone()[
            0
        ]
        == 100
    )


def test_two_request_disclosure_is_server_scoped_and_old_clients_keep_one(fixture_catalogue):
    from cartray.store import SqliteOrderStore, review_invitation_disclosure

    for configured, supplied, expected in [
        (2, "review-invitation-v2", 2),
        (2, "review-invitation-v1", 1),
        (1, "review-invitation-v2", 1),
    ]:
        store = SqliteOrderStore.in_memory()
        checkout = CheckoutService(
            fixture_catalogue,
            store,
            FakePaymentGateway(),
            review_invitations_enabled=True,
            review_invitation_default_selected=True,
            review_invitation_request_limit=configured,
        )
        request = CheckoutRequest(
            "disclosure", fixture_catalogue.version, (CanonicalItem("TEST-BUNDLE", 1),), True, supplied
        )
        run(checkout.checkout(request))
        row = store.connection.execute(
            "SELECT wording_version,wording FROM review_invitation_checkout_grants"
        ).fetchone()
        assert tuple(row) == review_invitation_disclosure(expected)
        store.connection.close()


@pytest.mark.parametrize("purchase", ["v1"], indirect=True)
def test_old_one_request_grant_cannot_be_expanded_to_both(purchase):
    db, env, sent, _ = purchase
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and sent[0]["reviews"]
    row = run(order_reviews.first(db, "SELECT settings_json,due_at FROM order_review_messages"))
    assert json.loads(row["settings_json"])["timing"] == "receipt" and row["due_at"] is None


def test_unsubscribe_preserves_the_next_purchase_receipt(purchase, fixture_catalogue):
    import time

    from cartray.models import CheckoutRedirect

    db, env, sent, _ = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "receipt"})
    run(worker._drain_post_purchase(env))
    original = run(order_reviews.first(db, "SELECT * FROM order_review_messages"))
    run(order_reviews.unsubscribe(db, "merchant", "test", original["unsubscribe_token"], int(time.time())))
    gateway = FakePaymentGateway(redirects={"previous": CheckoutRedirect("occupied", "https://checkout.invalid")})
    store = D1OrderStore(db)
    checkout = CheckoutService(
        fixture_catalogue, store, gateway, review_invitations_enabled=True, review_invitation_default_selected=True
    )
    redirect = run(
        checkout.checkout(
            CheckoutRequest("next-purchase", fixture_catalogue.version, (CanonicalItem("TEST-BUNDLE", 1),), True)
        )
    )
    order = gateway.requests[0].order_id
    run(
        store.confirm_settlement(
            order_id=order,
            session_id=redirect.session_id,
            event_id="evt_repeat",
            payload={"id": "evt_repeat"},
            payload_sha256="sha256:repeat",
            payment_status="paid",
            amount_total_minor=100,
            now=int(time.time()),
        )
    )
    run(worker._drain_post_purchase(env))
    assert len(sent) == 2 and sent[1]["type"] == "order_confirmation" and not sent[1].get("reviews")
    assert db.conn.execute("SELECT count(*) FROM order_review_messages").fetchone()[0] == 1


def test_withdrawn_recovery_is_retired_instead_of_starving_later_customers(purchase):

    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "receipt"})
    env.CARTRAY_REVIEW_INVITATION_KEY = "i" * 32
    run(worker._drain_post_purchase(env))
    db.conn.execute(
        "INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')", (order, "c" * 64)
    )
    db.conn.execute("UPDATE review_invitation_consents SET revoked_at=100 WHERE acquisition_id=?", (order,))
    db.conn.commit()
    run(worker._drain_post_purchase(env))
    assert db.conn.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "failed"
    assert len(sent) == 1
    # Repeated scheduled processing does not keep selecting an ineligible request.
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1


def test_receipt_includes_reviews_after_slow_recipient_evidence(purchase):
    db, env, sent, _ = purchase
    original = env.POST_PURCHASE_HANDLER.fetch

    async def slow_evidence(request):
        if json.loads(request.body)["type"] == "recipient_evidence":
            await asyncio.sleep(2.1)
        return await original(request)

    env.POST_PURCHASE_HANDLER.fetch = slow_evidence
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "receipt"})
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and sent[0]["reviews"]["subjects"]
    assert db.conn.execute("SELECT receipt_selection_json FROM order_review_messages").fetchone()[0]


def test_scheduled_notification_drain_is_awaited(purchase):
    db, env, sent, _ = purchase
    entry = worker.Default()
    entry.env = env
    # The scheduled argument is a raw JS execution context, not the SDK wrapper.
    # No coroutine is passed to its waitUntil as though it were a JS Promise.
    run(entry.scheduled(SimpleNamespace(cron="* * * * *"), env, object()))
    assert [message["type"] for message in sent] == ["order_confirmation", "review_followup"]
    assert db.conn.execute("SELECT state FROM order_review_messages").fetchone()[0] == "accepted"


def test_rejected_recovery_is_retired_before_later_requests(purchase, fixture_catalogue):
    from cartray.models import CheckoutRedirect

    db, env, sent, first_order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "receipt"})
    env.CARTRAY_REVIEW_INVITATION_KEY = "i" * 32
    run(worker._drain_post_purchase(env))
    gateway = FakePaymentGateway(redirects={"previous": CheckoutRedirect("occupied", "https://checkout.invalid")})
    store = D1OrderStore(db)
    checkout = CheckoutService(fixture_catalogue, store, gateway, review_invitations_enabled=True)
    redirect = run(checkout.checkout(CheckoutRequest(
        "later-recovery", fixture_catalogue.version, (CanonicalItem("TEST-BUNDLE", 1),), True)))
    later_order = gateway.requests[0].order_id
    run(store.confirm_settlement(order_id=later_order, session_id=redirect.session_id, event_id="evt_later",
                                 payload={"id":"evt_later"}, payload_sha256="sha256:later",
                                 payment_status="paid", amount_total_minor=100, now=2_000_000_000))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 2
    for order, when, key in ((first_order,100,'a'), (later_order,101,'b')):
        db.conn.execute("INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,?,'pending')",
                        (order,key*64,when))
    db.conn.commit()
    original = env.POST_PURCHASE_HANDLER.fetch

    async def reject_old_request(request):
        body = json.loads(request.body)
        if body['type'] == 'recipient_evidence' and body['order_id'] == first_order:
            return SimpleNamespace(status=422)
        return await original(request)

    env.POST_PURCHASE_HANDLER.fetch = reject_old_request
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    states = dict(db.conn.execute('SELECT acquisition_id,state FROM review_recovery_requests'))
    assert states == {first_order:'failed',later_order:'fulfilled'}
    assert len(sent) == 3 and sent[-1]['order_id'] == later_order


def test_recovery_reserves_send_budget_and_reuses_prepared_generation(purchase, monkeypatch):
    import time

    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing":"receipt"})
    env.CARTRAY_REVIEW_INVITATION_KEY = 'i'*32
    run(worker._drain_post_purchase(env))
    db.conn.execute("INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')",(order,'e'*64))
    db.conn.commit()
    clock = [100]
    monkeypatch.setattr(worker,'time',SimpleNamespace(time=time.time,monotonic=lambda:clock[0]))
    original = env.POST_PURCHASE_HANDLER.fetch

    async def slow_evidence(request):
        if json.loads(request.body)['type']=='recipient_evidence':
            clock[0] += 9
        return await original(request)

    env.POST_PURCHASE_HANDLER.fetch = slow_evidence
    run(worker._drain_review_recovery(env,env.POST_PURCHASE_HANDLER,env.CARTRAY_POST_PURCHASE_TOKEN,deadline=121))
    assert db.conn.execute('SELECT state FROM invitation_generations').fetchone()[0] == 'prepared'
    assert len(sent) == 1
    env.POST_PURCHASE_HANDLER.fetch = original
    clock[0] = 100
    run(worker._drain_review_recovery(env,env.POST_PURCHASE_HANDLER,env.CARTRAY_POST_PURCHASE_TOKEN,deadline=121))
    assert db.conn.execute('SELECT state FROM invitation_generations').fetchone()[0] == 'accepted'
    assert db.conn.execute('SELECT count(*) FROM invitation_generations').fetchone()[0] == 1
    assert len(sent) == 2


def test_recovery_cancellation_during_claim_is_held_without_resending(purchase,monkeypatch):
    import time

    from cartray_reviews import invitations

    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing":"receipt"})
    env.CARTRAY_REVIEW_INVITATION_KEY = 'i'*32
    run(worker._drain_post_purchase(env))
    db.conn.execute("INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')",(order,'f'*64))
    db.conn.commit()
    original = invitations.claim_delivery

    async def committed_then_cancelled(*args,**kwargs):
        await original(*args,**kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(invitations,'claim_delivery',committed_then_cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(worker._drain_review_recovery(env,env.POST_PURCHASE_HANDLER,env.CARTRAY_POST_PURCHASE_TOKEN,
                                         deadline=time.monotonic()+21))
    assert db.conn.execute('SELECT state FROM invitation_generations').fetchone()[0] == 'dispatching'
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1


@pytest.mark.parametrize('status',[400,403,429,502,503])
def test_recovery_auth_or_upstream_failure_stays_pending(purchase,status):
    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({'timing':'receipt'})
    run(worker._drain_post_purchase(env))
    db.conn.execute("INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')",(order,'d'*64))
    db.conn.commit()

    async def temporarily_unavailable(request):
        return SimpleNamespace(status=status)

    env.POST_PURCHASE_HANDLER.fetch = temporarily_unavailable
    run(worker._drain_post_purchase(env))
    assert db.conn.execute('SELECT state FROM review_recovery_requests').fetchone()[0] == 'pending'
    assert db.conn.execute('SELECT count(*) FROM invitation_generations').fetchone()[0] == 0
    assert len(sent) == 1


def test_concurrent_recovery_loser_cannot_overwrite_winning_send(purchase,monkeypatch):
    import time

    from cartray_reviews import invitations

    db, env, sent, order = purchase
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({'timing':'receipt'})
    env.CARTRAY_REVIEW_INVITATION_KEY = 'i'*32
    run(worker._drain_post_purchase(env))
    db.conn.execute("INSERT INTO review_recovery_requests VALUES ('merchant','test',?,?,100,'pending')",(order,'9'*64))
    db.conn.commit()

    async def scenario():
        both_prepared, sending, release, loser = (asyncio.Event() for _ in range(4))
        # D1 serializes transactions. The SQLite test adapter shares a single
        # connection, so serialize its batches rather than nesting transactions.
        batch_lock, original_batch = asyncio.Lock(), db.batch

        async def serial_batch(statements):
            async with batch_lock:
                return await original_batch(statements)

        monkeypatch.setattr(db,'batch',serial_batch)
        prepared = 0
        prepare, claim = invitations.prepare_replacement,invitations.claim_delivery
        mail = env.POST_PURCHASE_HANDLER.fetch

        async def concurrent_prepare(*args,**kwargs):
            nonlocal prepared
            result = await prepare(*args,**kwargs)
            prepared += 1
            if prepared == 2:
                both_prepared.set()
            await both_prepared.wait()
            return result

        async def observed_claim(*args,**kwargs):
            try:
                return await claim(*args,**kwargs)
            except ValueError:
                loser.set()
                raise

        async def held_send(request):
            if json.loads(request.body)['type']=='review_followup':
                sending.set()
                await release.wait()
            return await mail(request)

        monkeypatch.setattr(invitations,'prepare_replacement',concurrent_prepare)
        monkeypatch.setattr(invitations,'claim_delivery',observed_claim)
        env.POST_PURCHASE_HANDLER.fetch = held_send
        tasks = [asyncio.create_task(worker._drain_review_recovery(
            env,env.POST_PURCHASE_HANDLER,env.CARTRAY_POST_PURCHASE_TOKEN,deadline=time.monotonic()+21))
            for _ in range(2)]
        try:
            await asyncio.wait_for(sending.wait(),2)
            await asyncio.wait_for(loser.wait(),2)
            assert db.conn.execute('SELECT state FROM invitation_generations').fetchone()[0] == 'dispatching'
        finally:
            release.set()
            await asyncio.gather(*tasks)

    run(scenario())
    assert db.conn.execute('SELECT state FROM invitation_generations').fetchone()[0] == 'accepted'
    assert db.conn.execute('SELECT count(*) FROM invitation_generations').fetchone()[0] == 1
    assert len(sent) == 2
