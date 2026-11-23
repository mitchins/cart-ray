"""Local-only Workerd smoke target for CartRay's D1 and Web Crypto boundaries."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

from js import Object, Request, crypto
from js import Response as JsResponse
from kinglet.storage import bytes_to_arraybuffer
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from cartray.errors import CheckoutInProgress
from cartray.models import CheckoutOrder, CheckoutRedirect, OrderItem
from cartray.order_access import issue_order_access, issuer_token, purchased_resources
from cartray.post_purchase import OrderReceiptDispatcher
from cartray.store import D1OrderStore, _results
from cartray.worker import _drain_post_purchase, create_app
from cartray.workers_crypto import WorkersEd25519Signer, WorkersEd25519Verifier

TEST_PRIVATE_KEY_PKCS8_B64 = "MC4CAQAwBQYDK2VwBCIEIJZ4orlcGRX4w6SOrvHeEh5ZpFSC0FR4Onu1OKF/Lt87"
TEST_PUBLIC_KEY_RAW_B64 = "mjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PUBLIC_KEY_SPKI_B64 = "MCowBQYDK2VwAyEAmjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PAYLOAD = b"cartray-worker-webcrypto-smoke-v1"


async def order_reviews_journey(db):
    """Real D1/native submission, synthetic mail: the link works BEFORE acceptance."""
    import hashlib
    import hmac
    import secrets

    from cartray_reviews.invitations import redeem_purchase

    from cartray import order_reviews
    now = int(time.time())
    suffix = str(time.time_ns())
    order = CheckoutOrder(order_id='cr_reviews_'+suffix, checkout_request_id='reviews_'+suffix,
        request_fingerprint='fp_'+suffix, manifest_version='smoke-review-version',
        items=(OrderItem(product_key='REVIEW-A',quantity=2,stripe_price_id='price_a',unit_amount_minor=1,
                         fulfilment_resources=('smoke-resource',)),
               OrderItem(product_key='REVIEW-B',quantity=1,stripe_price_id='price_b',unit_amount_minor=1,
                         fulfilment_resources=('smoke-external',))),
        items_digest='sha256:'+'2'*64,currency='aud',subtotal_minor=3,
        review_invitation_opt_in=True,review_invitation_policy_enabled=True,
        review_invitation_default_selected=True,review_invitation_request_limit=2)
    store = D1OrderStore(db)
    await store.start_or_load(order, nonce='reviews', now=now)
    await store.attach_redirect(order.order_id, CheckoutRedirect('cs_test_'+suffix,'https://stripe.test'), now=now)
    await store.confirm_settlement(order_id=order.order_id,session_id='cs_test_'+suffix,
        event_id='evt_'+suffix,payload={'id':'evt_'+suffix},payload_sha256='sha256:smoke',
        payment_status='paid',amount_total_minor=3,now=now)
    key = 'h'*32
    check = hmac.new(key.encode(), json.dumps(['reviews-suppression-key-check-v1','smoke-store','test',
        'smoke-install','key1'],separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    await db.prepare("INSERT OR IGNORE INTO review_suppression_key_registry "
        "VALUES ('smoke-store','test','smoke-install','key1',?)").bind(check).run()
    await db.prepare("INSERT OR IGNORE INTO review_suppression_key_current "
        "VALUES ('smoke-store','test','smoke-install','key1')").run()
    for subject in ('review-a','review-b'):
        await (db.prepare("INSERT OR IGNORE INTO review_subjects VALUES ('smoke-store','test',?,'product',?)")
            .bind(subject,'/'+subject+'/').run())
        await (db.prepare("INSERT OR IGNORE INTO review_subject_display_names VALUES ('smoke-store','test',?,?)")
            .bind(subject,subject).run())
    messages = []
    class Mail:
        async def fetch(self, request):
            envelope = json.loads(await request.text())
            if envelope['type'] == 'recipient_evidence':
                return JsResponse.json(to_js({'email':'buyer-'+suffix+'@example.test'},dict_converter=Object.fromEntries))
            messages.append(envelope)
            if envelope['type'] == 'order_confirmation':
                bearer = envelope['reviews']['url'].split('invitation=')[1].split('&')[0]
                await redeem_purchase(db, store='smoke-store',environment='test',subject='review-a',token=bearer,
                    request_key='review_'+suffix,author='Buyer',title='',body='Helpful',rating=5,
                    reviewer_key_secret=b'r'*32,now=now,random_token=lambda:secrets.token_hex(32))
            return JsResponse.json(to_js({'provider_reference':'provider_'+suffix},dict_converter=Object.fromEntries))
    env = SimpleNamespace(DB=db,CARTRAY_ENVIRONMENT='test',CARTRAY_POST_PURCHASE_MODE='test',
        CARTRAY_MERCHANT_ID='smoke-store',CARTRAY_POST_PURCHASE_TOKEN='t'*32,
        CARTRAY_STOREFRONT_ORIGIN='https://shop.example',POST_PURCHASE_HANDLER=Mail(),
        CARTRAY_RECIPIENT_HASH_KEY=key,CARTRAY_INSTALLATION_ID='smoke-install',
        CARTRAY_REVIEW_SETTINGS=json.dumps({'timing':'both','delay_days':0,'priorities':['REVIEW-A']}),
        CARTRAY_REVIEW_SUBJECT_MAPS=json.dumps({'smoke-review-version':{'REVIEW-A':'review-a','REVIEW-B':'review-b'}}))
    await _drain_post_purchase(env)
    await _drain_post_purchase(env)
    if len(messages)!=2 or [m['type'] for m in messages]!=['order_confirmation','review_followup']:
        return False
    message = await order_reviews.first(db,'SELECT * FROM order_review_messages WHERE order_id=?',order.order_id)
    await order_reviews.unsubscribe(db,'smoke-store','test',message['unsubscribe_token'],now)
    return (messages[0]['reviews']['subjects']==['review-a'] and messages[1]['reviews']['subjects']==['review-b']
        and not await order_reviews.permitted(db,'smoke-store','test',order.order_id,message['recipient_hash']))


class Default(WorkerEntrypoint):
    def order_env(self):
        async def get_file(key):
            return SimpleNamespace(body=JsResponse.new("workerd private file").body) if key == "test" else None

        async def allow(_value):
            return {"success": True}

        return SimpleNamespace(
            CARTRAY_ORDER_ACCESS_MODE="test", CARTRAY_ENVIRONMENT="test",
            CARTRAY_MERCHANT_ID="smoke-store", CARTRAY_SIGNING_KEY_ID="smoke-install", DB=self.env.DB,
            CARTRAY_RESOURCE_CONFIG=json.dumps({
                "smoke-resource": {"provider": "private", "binding": "FILES", "key": "test",
                                   "filename": "example.txt", "mime_type": "text/plain"},
                "smoke-external": {"provider": "external", "url": "https://example.test/resource",
                                   "filename": "External resource"},
            }),
            CHECKOUT_STATUS_RATE_LIMITER=SimpleNamespace(limit=allow), FILES=SimpleNamespace(get=get_file),
        )

    async def fetch(self, _request):
        if "/order-access" in str(_request.url):
            headers = {"cf-connecting-ip": "192.0.2.1"}
            options = {"method": _request.method, "headers": headers}
            if _request.method == "POST":
                body = await _request.text()
                headers["content-type"] = _request.headers.get("content-type")
                headers["content-length"] = str(len(body.encode()))
                options["body"] = body
            request = Request.new(str(_request.url), to_js(options, dict_converter=Object.fromEntries))
            return await create_app()(request, self.order_env())
        await self.env.DB.prepare(
            """INSERT OR IGNORE INTO post_purchase_checkpoint
            SELECT 1,'smoke-store','test',COALESCE(MAX(id),0),COALESCE(MAX(id),0) FROM outbox"""
        ).run()
        token = str(time.time_ns())
        order = CheckoutOrder(
            order_id=f"cr_smoke_{token}",
            checkout_request_id=f"smoke_{token}",
            request_fingerprint=f"fingerprint_{token}",
            manifest_version="sha256:" + "0" * 64,
            items=(
                OrderItem(
                    product_key="SMOKE-TEMPLATE",
                    quantity=1,
                    stripe_price_id="price_smoke",
                    unit_amount_minor=1,
                    fulfilment_resources=("smoke-resource", "smoke-external"),
                ),
            ),
            items_digest="sha256:" + "1" * 64,
            currency="aud",
            subtotal_minor=1,
        )
        store = D1OrderStore(self.env.DB, lease_seconds=60)
        first = await store.start_or_load(order, nonce="smoke-nonce", now=10)
        lease_rejected = False
        try:
            await store.start_or_load(order, nonce="contender", now=10)
        except CheckoutInProgress:
            lease_rejected = True
        redirect = CheckoutRedirect(f"cs_smoke_{token}", "https://checkout.stripe.test/smoke")
        await store.attach_redirect(order.order_id, redirect, now=11)
        confirmed = await store.confirm_settlement(
            order_id=order.order_id,
            session_id=redirect.session_id,
            event_id=f"evt_smoke_{token}",
            payload={"id": f"evt_smoke_{token}"},
            payload_sha256="sha256:smoke",
            payment_status="paid",
            amount_total_minor=1,
            now=12,
        )
        outbox = (
            await self.env.DB.prepare("SELECT event_type FROM outbox WHERE order_id = ? ORDER BY id")
            .bind(order.order_id)
            .all()
        )
        session = (
            await self.env.DB.prepare(
                "SELECT external_session_id, redirect_url FROM checkout_sessions WHERE order_id = ?"
            )
            .bind(order.order_id)
            .all()
        )

        await self.env.DB.prepare(
            "INSERT OR IGNORE INTO post_purchase_installation VALUES (1,'smoke-store','test')"
        ).run()
        calls = []

        async def receipt_handler(purchase):
            calls.append(purchase.order_id)
            return "test-provider-reference"

        dispatcher = OrderReceiptDispatcher(self.env.DB, "smoke-store", "test", {"receipt": receipt_handler})
        budget = await dispatcher.dispatch(order.order_id, (), max_new_claims=0)
        budget_deferred = budget == {"receipt": "deferred"}
        runs = await asyncio.gather(
            dispatcher.dispatch(order.order_id, ()), dispatcher.dispatch(order.order_id, ())
        )
        replay = await dispatcher.dispatch(order.order_id, ())
        service_calls = []

        class TestService:
            async def fetch(self, request):
                service_calls.append(1)

                async def response_json():
                    return {"provider_reference": "test-service-receipt"}

                return SimpleNamespace(status=200, json=response_json)

        test_env = SimpleNamespace(
            DB=self.env.DB, CARTRAY_ENVIRONMENT="test", CARTRAY_POST_PURCHASE_MODE="test",
            CARTRAY_POST_PURCHASE_TOKEN="t" * 32, CARTRAY_MERCHANT_ID="smoke-store",
            POST_PURCHASE_HANDLER=TestService(),
        )
        await _drain_post_purchase(test_env)
        await _drain_post_purchase(test_env)
        receipt_rows = _results(await self.env.DB.prepare(
            "SELECT workflow_key,state FROM post_purchase_executions WHERE order_id=?"
        ).bind(order.order_id).all())
        workflows_once = (calls == [order.order_id] and replay == {"receipt": "already_claimed"}
                          and not service_calls and len(receipt_rows) == 1
                          and receipt_rows[0] == {"workflow_key": "order-receipt", "state": "accepted"}
                          and all(set(result) == {"receipt"} for result in runs))

        access = await issue_order_access(self.env.DB, "s" * 32, "smoke-store", "test", order.order_id)
        access_items = await purchased_resources(self.env.DB, access, "smoke-store", "test")
        order_access_ok = access_items == [{"product_key": "SMOKE-TEMPLATE", "quantity": 1,
                                           "resources": ["smoke-resource", "smoke-external"]}]
        issue_body = json.dumps({"order_id": order.order_id})
        issue_env = SimpleNamespace(
            CARTRAY_ORDER_ACCESS_MODE="test", CARTRAY_ENVIRONMENT="test", CARTRAY_MERCHANT_ID="smoke-store",
            CARTRAY_ORDER_ACCESS_KEY="s" * 32, DB=self.env.DB,
        )
        issue_request = Request.new(
            "https://test.invalid/internal/order-access",
            to_js({"method": "POST", "headers": {"authorization": "Bearer " + issuer_token("s" * 32),
                   "content-length": str(len(issue_body)), "content-type": "application/json"}, "body": issue_body},
                  dict_converter=Object.fromEntries),
        )
        issued_response = await create_app()(issue_request, issue_env)
        order_access_ok = order_access_ok and issued_response.status == 200
        if str(_request.url).endswith("?browser=1"):
            return Response(None, status=302, headers={"Location": "/order-access#token=" + access})
        if str(_request.url).endswith("?download=1"):
            form_body = "token=" + access + "&resource=0.0"
            raw = Request.new("https://test.invalid/order-access", to_js({"method": "POST",
                "headers": {"cf-connecting-ip": "192.0.2.1", "content-type": "application/x-www-form-urlencoded",
                            "content-length": str(len(form_body))}, "body": form_body},
                dict_converter=Object.fromEntries))
            return await create_app()(raw, self.order_env())

        signer = WorkersEd25519Signer(TEST_PRIVATE_KEY_PKCS8_B64)
        signature = await signer.sign(TEST_PAYLOAD)
        public_key = await crypto.subtle.importKey(
            "raw",
            bytes_to_arraybuffer(base64.b64decode(TEST_PUBLIC_KEY_RAW_B64)),
            {"name": "Ed25519"},
            False,
            to_js(["verify"]),
        )
        signature_verified = await crypto.subtle.verify(
            "Ed25519", public_key, bytes_to_arraybuffer(signature), bytes_to_arraybuffer(TEST_PAYLOAD)
        )
        verifier = WorkersEd25519Verifier(TEST_PUBLIC_KEY_SPKI_B64)
        verifier_accepted = await verifier.verify(TEST_PAYLOAD, signature)
        session_rows = _results(session)
        reviews_ok = await order_reviews_journey(self.env.DB)
        result = {
            "order_reviews": reviews_ok,
            "d1_batch_owner": first.owner,
            "first_confirmation": confirmed is False,
            "lease_rejected": lease_rejected,
            "outbox_events": [row["event_type"] for row in _results(outbox)],
            "redirect_persisted": len(session_rows) == 1
            and session_rows[0]["external_session_id"] == redirect.session_id
            and session_rows[0]["redirect_url"] == redirect.url,
            "signature_verified": bool(signature_verified),
            "verifier_accepted": verifier_accepted,
            "workflows_once": workflows_once,
            "budget_deferred": budget_deferred,
            "order_access": order_access_ok,
        }
        expected = {
            "order_reviews": True,
            "d1_batch_owner": True,
            "first_confirmation": True,
            "lease_rejected": True,
            "outbox_events": ["OrderCreated", "CheckoutRedirectIssued", "OrderConfirmed"],
            "redirect_persisted": True,
            "signature_verified": True,
            "verifier_accepted": True,
            "workflows_once": True,
            "budget_deferred": True,
            "order_access": True,
        }
        return Response.json(result, status=200 if result == expected else 500)
