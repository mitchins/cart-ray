"""A synthetic purchase exercises the existing outbox and one receipt claim."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from kinglet import MockD1Database

import cartray.worker as worker
from cartray.canonical import CanonicalItem
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRequest
from cartray.order_access import purchased_resources
from cartray.store import D1OrderStore


def run(value):
    return asyncio.run(value)


def test_confirmed_mixed_order_sends_one_receipt_even_when_replayed(fixture_catalogue, monkeypatch):
    db = MockD1Database()
    for migration in sorted((Path(__file__).parents[1] / 'migrations').glob('*.sql')):
        db.conn.executescript(migration.read_text())
    db.conn.execute("INSERT INTO post_purchase_installation VALUES (1,'merchant','test')")
    db.conn.execute("INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','test',0,0)")
    db.conn.commit()
    gateway = FakePaymentGateway()
    store = D1OrderStore(db)
    checkout = CheckoutService(fixture_catalogue, store, gateway)
    request = CheckoutRequest('mixed-receipt', fixture_catalogue.version,
                              (CanonicalItem('TEST-BUNDLE', 1), CanonicalItem('TEST-SUPPORT-HOURS', 2)))
    redirect = run(checkout.checkout(request))
    order_id = gateway.requests[0].order_id
    event = {'order_id': order_id, 'session_id': redirect.session_id, 'event_id': 'evt_receipt',
             'payload': {'id': 'evt_receipt'}, 'payload_sha256': 'sha256:fixture',
             'payment_status': 'paid', 'amount_total_minor': 100, 'now': 100}
    assert run(store.confirm_settlement(**event)) is False
    assert run(store.confirm_settlement(**event)) is True
    sent = []

    class Service:
        async def fetch(self, request):
            sent.append(json.loads(request.body))

            async def response_json():
                return {'provider_reference': 'test-provider-id'}

            return SimpleNamespace(status=200, json=response_json)

    monkeypatch.setattr(worker, '_internal_post', lambda _url, _token, body: SimpleNamespace(body=body))
    env = SimpleNamespace(DB=db, CARTRAY_ENVIRONMENT='test', CARTRAY_POST_PURCHASE_MODE='test',
                          CARTRAY_POST_PURCHASE_TOKEN='t' * 32, CARTRAY_MERCHANT_ID='merchant',
                          CARTRAY_ORDER_ACCESS_MODE='test', CARTRAY_ORDER_ACCESS_KEY='s' * 32,
                          CARTRAY_STOREFRONT_ORIGIN='https://shop.example', POST_PURCHASE_HANDLER=Service())
    run(worker._drain_post_purchase(env))
    run(worker._drain_post_purchase(env))
    assert len(sent) == 1 and sent[0]['type'] == 'order_confirmation'
    assert [(item['product_key'], item['quantity']) for item in sent[0]['items']] == [
        ('TEST-BUNDLE', 1), ('TEST-SUPPORT-HOURS', 2)
    ]
    assert sent[0]['access_url'].startswith('https://shop.example/order-access#token=')
    token = sent[0]['access_url'].split('#token=', 1)[1]
    items = run(purchased_resources(db, token, 'merchant', 'test'))
    assert [item['resources'] for item in items] == [
        ['TEST-RESOURCE-A', 'TEST-RESOURCE-B'], ['TEST-RESOURCE-SUPPORT-HOURS']
    ]
    assert db.conn.execute('SELECT count(*) FROM post_purchase_executions').fetchone()[0] == 1
    db.conn.close()
