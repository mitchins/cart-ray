"""Optional store reviews reuse paid-order capabilities and the normal mail path."""

import json
import secrets
from types import SimpleNamespace

import pytest
from kinglet import TestClient
from test_order_reviews import purchase as purchase
from test_order_reviews import run

from cartray import order_reviews, worker
from cartray.canonical import CanonicalItem
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRedirect, CheckoutRequest
from cartray.store import D1OrderStore


def enable_store(db, env):
    db.conn.execute("INSERT INTO review_subjects VALUES ('merchant','test','merchant-store','store','/reviews/')")
    db.conn.execute("INSERT INTO review_subject_display_names VALUES ('merchant','test','merchant-store','The Shop')")
    db.conn.execute("INSERT INTO native_publication_versions "
                    "(store_key,environment,subject_id,version,contribution_count,rated_count,rating_sum) "
                    "VALUES ('merchant','test','merchant-store',0,0,0,0)")
    db.conn.commit()
    env.CARTRAY_STORE_REVIEW_SUBJECT = json.dumps(
        {"subject_id": "merchant-store", "path": "/reviews/", "name": "The Shop"})
    settings = json.loads(env.CARTRAY_REVIEW_SETTINGS)
    env.CARTRAY_REVIEW_SETTINGS = json.dumps(settings | {"delay_days": 7})


def invitation(db, order):
    return db.conn.execute(
        "SELECT invitation_token FROM order_review_messages WHERE order_id=?", (order,)).fetchone()[0]


def review(db, token, subject, key):
    from cartray_reviews.invitations import redeem_purchase

    return run(redeem_purchase(
        db, store="merchant", environment="test", subject=subject, token=token, request_key=key,
        author="Buyer", title="", body="A useful experience", rating=5, reviewer_key_secret=b"r" * 32,
        now=200, random_token=lambda: secrets.token_hex(32)))


def access_client(db, env, order):
    env.CARTRAY_SIGNING_KEY_ID = 'installation'
    env.CARTRAY_ORDER_ACCESS_MODE = 'test'
    env.CARTRAY_RESOURCE_CONFIG = '{}'
    token = run(worker.issue_order_access(db, 'a' * 32, 'merchant', 'test', order))

    async def allow(_):
        return {"success": True}

    env.CHECKOUT_STATUS_RATE_LIMITER = SimpleNamespace(limit=allow)
    client = TestClient(worker.create_app(), env=vars(env))
    body = {"token": token}
    headers = {"content-type": "application/json", "content-length": str(len(json.dumps(body).encode())),
               "cf-connecting-ip": "192.0.2.42"}

    def read(value=body):
        status, _, payload = client.request('POST', '/order-access', json_data=value, headers=headers)
        return status, json.loads(payload)

    return read, token


@pytest.mark.parametrize('purchase', ['v4'], indirect=True)
def test_mixed_paid_order_has_store_once_and_all_products_under_one_link(purchase):
    db, env, sent, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and sent[0]['type'] == 'order_confirmation'
    assert sent[0]['reviews']['subjects'] == ['second']  # Featuring never restricts the capability.
    subjects = db.conn.execute('SELECT subject_id FROM acquisition_invitation_subjects ORDER BY subject_id').fetchall()
    assert [row[0] for row in subjects] == ['first', 'merchant-store', 'second']
    assert db.conn.execute('SELECT COUNT(*) FROM acquisition_invitations').fetchone()[0] == 1
    token = invitation(db, order)
    result = review(db, token, 'merchant-store', 'store-once')
    assert result['status'] == 'pending'
    assert review(db, token, 'merchant-store', 'store-once') == result
    with pytest.raises(ValueError):
        review(db, token, 'merchant-store', 'store-again')
    assert review(db, token, 'first', 'product-once')['status'] == 'pending'
    row = run(order_reviews.first(db, 'SELECT * FROM order_review_messages'))
    available = run(order_reviews.outstanding(db, 'merchant', 'test', order))
    assert available == ['second']
    assert run(worker._review_message_links(env, row))['subjects'] == ['second']


@pytest.mark.parametrize('purchase', ['v4-declined'], indirect=True)
def test_declined_email_permission_preserves_deliberate_authorised_review_access(purchase):
    db, env, sent, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and not sent[0].get('reviews')
    assert db.conn.execute('SELECT COUNT(*) FROM acquisition_invitations').fetchone()[0] == 0
    read, _ = access_client(db, env, order)
    status, data = read()
    assert status == 200 and data['reviews']['subjects'] == ['second']
    assert db.conn.execute('SELECT COUNT(*) FROM acquisition_invitation_subjects').fetchone()[0] == 3
    assert db.conn.execute('SELECT COUNT(*) FROM review_invitation_checkout_grants').fetchone()[0] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM review_invitation_consents').fetchone()[0] == 0
    assert review(db, invitation(db, order), 'merchant-store', 'voluntary-store')['status'] == 'pending'
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1
    assert db.conn.execute('SELECT due_at FROM order_review_messages').fetchone()[0] is None
    assert read({"token": '0' * 64})[0] != 200


@pytest.mark.parametrize('purchase', ['v4'], indirect=True)
def test_unsubscribe_revokes_notifications_not_existing_order_authority(purchase):
    db, env, sent, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    message = run(order_reviews.first(db, 'SELECT * FROM order_review_messages'))
    run(order_reviews.unsubscribe(db, 'merchant', 'test', message['unsubscribe_token'], 200))
    assert db.conn.execute('SELECT revoked_at FROM review_invitation_consents').fetchone()[0] == 200
    assert not run(order_reviews.permitted(db, 'merchant', 'test', order, message['recipient_hash']))
    assert run(worker._review_message_links(env, message)) is None
    read, _ = access_client(db, env, order)
    assert read()[1]['reviews']['url']
    assert review(db, invitation(db, order), 'merchant-store', 'suppressed-voluntary')['status'] == 'pending'
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1
    db.conn.execute("UPDATE acquisition_invitations SET revoked_at=201")
    db.conn.commit()
    assert 'reviews' not in read()[1]


@pytest.mark.parametrize('purchase', ['v1', 'restricted-v2', None], indirect=True)
def test_old_product_only_disclosures_never_gain_store_authority(purchase):
    db, env, sent, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    assert db.conn.execute(
        "SELECT COUNT(*) FROM acquisition_invitation_subjects WHERE subject_id='merchant-store'"
    ).fetchone()[0] == 0
    assert review(db, invitation(db, order), 'first', 'legacy-product')['status'] == 'pending'
    with pytest.raises(ValueError):
        review(db, invitation(db, order), 'merchant-store', 'legacy-store')


@pytest.mark.parametrize('purchase', ['v4'], indirect=True)
def test_issued_product_capability_is_not_expanded_by_enabling_store(purchase):
    db, env, sent, order = purchase
    run(worker._drain_post_purchase(env))
    old = invitation(db, order)
    enable_store(db, env)
    read, _ = access_client(db, env, order)
    assert read()[0] == 200
    assert invitation(db, order) == old
    assert db.conn.execute('SELECT COUNT(*) FROM acquisition_invitation_subjects').fetchone()[0] == 2
    with pytest.raises(ValueError):
        review(db, old, 'merchant-store', 'cannot-widen')


@pytest.mark.parametrize('purchase', ['v4-declined'], indirect=True)
def test_request_timing_off_does_not_disable_voluntary_v4_order_access(purchase):
    db, env, sent, order = purchase
    enable_store(db, env)
    env.CARTRAY_REVIEW_SETTINGS = json.dumps({"timing": "off"})
    run(worker._drain_post_purchase(env))
    read, _ = access_client(db, env, order)
    assert read()[1]['reviews']['url']
    assert review(db, invitation(db, order), 'merchant-store', 'timing-off')['status'] == 'pending'
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and not sent[0].get('reviews')


@pytest.mark.parametrize('purchase', ['v4'], indirect=True)
def test_later_paid_order_has_its_own_store_review(purchase, fixture_catalogue):
    db, env, _, first_order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    review(db, invitation(db, first_order), 'merchant-store', 'first-purchase-store')
    gateway = FakePaymentGateway(redirects={"prior": CheckoutRedirect("prior", "https://checkout.invalid/prior")})
    store = D1OrderStore(db)
    checkout = CheckoutService(fixture_catalogue, store, gateway, review_invitations_enabled=True)
    redirect = run(checkout.checkout(CheckoutRequest('later-paid-order', fixture_catalogue.version,
        (CanonicalItem('TEST-BUNDLE', 1),), review_invitation_opt_in=True,
        review_invitation_wording_version='review-invitation-v4')))
    order = gateway.requests[0].order_id
    run(store.confirm_settlement(order_id=order, session_id=redirect.session_id, event_id='evt_later',
        payload={'id': 'evt_later'}, payload_sha256='sha256:later', payment_status='paid',
        amount_total_minor=100, now=2_000_000_000))
    run(worker._drain_post_purchase(env))
    assert review(db, invitation(db, order), 'merchant-store', 'later-purchase-store')['status'] == 'pending'
    assert db.conn.execute(
        "SELECT COUNT(*) FROM native_reviews WHERE subject_id='merchant-store'"
    ).fetchone()[0] == 2
    assert db.conn.execute(
        "SELECT COUNT(DISTINCT reviewer_key) FROM native_reviews WHERE subject_id='merchant-store'"
    ).fetchone()[0] == 2


@pytest.mark.parametrize('purchase', ['v4-declined'], indirect=True)
def test_missing_or_wrong_recipient_evidence_does_not_create_voluntary_capability(purchase):
    db, env, _, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))

    class Unavailable:
        async def fetch(self, _):
            return SimpleNamespace(status=503)

    env.POST_PURCHASE_HANDLER = Unavailable()
    read, _ = access_client(db, env, order)
    status, data = read()
    assert status == 200 and data['items'] and 'reviews' not in data
    assert db.conn.execute('SELECT COUNT(*) FROM acquisition_invitations').fetchone()[0] == 0


def test_cached_bool_only_checkout_does_not_gain_store_scope_and_remains_idempotent(purchase, fixture_catalogue):
    db, env, _, _ = purchase
    enable_store(db, env)
    gateway = FakePaymentGateway(redirects={"prior": CheckoutRedirect("prior", "https://checkout.invalid/prior")})
    store = D1OrderStore(db)
    checkout = CheckoutService(fixture_catalogue, store, gateway, review_invitations_enabled=True)
    payload = {"checkout_request_id": "cached-client", "manifest_version": fixture_catalogue.version,
               "items": [{"product_key": "TEST-BUNDLE", "quantity": 1}], "review_invitation_opt_in": True}
    request = CheckoutRequest.from_payload(payload)
    assert request.review_invitation_wording_version == 'review-invitation-v1'
    redirect = run(checkout.checkout(request))
    assert run(checkout.checkout(CheckoutRequest.from_payload(payload))) == redirect
    assert len(gateway.requests) == 1
    order = gateway.requests[0].order_id
    run(store.confirm_settlement(order_id=order, session_id=redirect.session_id, event_id='evt_cached',
        payload={'id': 'evt_cached'}, payload_sha256='sha256:cached', payment_status='paid',
        amount_total_minor=100, now=2_000_000_000))
    run(worker._drain_post_purchase(env))
    subjects = db.conn.execute(
        'SELECT subject_id FROM acquisition_invitation_subjects WHERE acquisition_id=?', (order,)).fetchall()
    assert [row[0] for row in subjects] == ['first']
    grant = db.conn.execute('SELECT wording_version,request_limit FROM review_invitation_checkout_grants '
                            'WHERE order_id=?', (order,)).fetchone()
    assert tuple(grant) == ('review-invitation-v1', 1)


@pytest.mark.parametrize('purchase', ['v4'], indirect=True)
def test_store_review_edit_withdraw_preserves_once_per_order(purchase):
    from cartray_reviews.management import manage_review, management_token

    db, env, _, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    token = invitation(db, order)
    created = review(db, token, 'merchant-store', 'managed-store')
    secret = b'r' * 32
    capability = management_token(secret, store='merchant', environment='test', review_id=created['review_id'])

    def manage(action, revision, data, key):
        return run(manage_review(db, store='merchant', environment='test', review_id=created['review_id'],
            capability=capability, secret=secret, action=action, revision=revision, data=data,
            request_key=key, now=300, random_token=lambda: secrets.token_hex(32)))

    edited = manage('edit', created['revision_id'], {'author': 'Buyer', 'body': 'Corrected experience',
                                                     'rating': 4}, 'edit-store')
    withdrawn = manage('withdraw', edited['revision_id'], {}, 'withdraw-store')
    assert withdrawn['status'] == 'withdrawn'
    assert manage('withdraw', edited['revision_id'], {}, 'withdraw-store') == withdrawn
    with pytest.raises(ValueError):
        review(db, token, 'merchant-store', 'withdraw-not-new-review')
    assert 'merchant-store' not in run(order_reviews.outstanding(db, 'merchant', 'test', order))


@pytest.mark.parametrize('path', ['/cart/', '/review/', '/reviews/v1/', '/checkout/'])
def test_store_subject_cannot_claim_application_route(path):
    with pytest.raises(ValueError):
        order_reviews.StoreReviewSubject.parse({'subject_id': 'shop', 'path': path, 'name': 'The Shop'})


@pytest.mark.parametrize('purchase', ['v4-declined'], indirect=True)
def test_order_access_keeps_management_link_when_every_subject_is_consumed(purchase):
    db, env, sent, order = purchase
    enable_store(db, env)
    run(worker._drain_post_purchase(env))
    read, _ = access_client(db, env, order)
    assert read()[0] == 200
    token = invitation(db, order)
    for subject in ('first', 'second', 'merchant-store'):
        review(db, token, subject, 'complete-' + subject)
    status, data = read()
    assert status == 200 and data['reviews']['url']
    assert data['reviews']['subjects'] == []
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1
