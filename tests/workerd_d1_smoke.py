"""Local-only Workerd smoke target for CartRay's D1 and Web Crypto boundaries."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

from js import crypto
from kinglet.storage import bytes_to_arraybuffer
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from cartray.errors import CheckoutInProgress
from cartray.models import CheckoutOrder, CheckoutRedirect, OrderItem
from cartray.post_purchase import Action, D1Dispatcher
from cartray.store import D1OrderStore, _results
from cartray.worker import _drain_post_purchase
from cartray.workers_crypto import WorkersEd25519Signer, WorkersEd25519Verifier

TEST_PRIVATE_KEY_PKCS8_B64 = "MC4CAQAwBQYDK2VwBCIEIJZ4orlcGRX4w6SOrvHeEh5ZpFSC0FR4Onu1OKF/Lt87"
TEST_PUBLIC_KEY_RAW_B64 = "mjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PUBLIC_KEY_SPKI_B64 = "MCowBQYDK2VwAyEAmjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PAYLOAD = b"cartray-worker-webcrypto-smoke-v1"


class Default(WorkerEntrypoint):
    async def fetch(self, _request):
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
                    fulfilment_resources=("smoke-resource",),
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

        async def action_handler(purchase, execution):
            calls.append((purchase.order_id, execution.workflow_key))
            return "test-provider-reference"

        actions = (
            Action("workflow-one", ("SMOKE-TEMPLATE",), "email", "test/one", "a" * 64),
            Action("workflow-two", ("SMOKE-TEMPLATE",), "email", "test/two", "b" * 64),
        )
        dispatcher = D1Dispatcher(self.env.DB, "smoke-store", "test", {"email": action_handler})
        runs = await asyncio.gather(
            dispatcher.dispatch(order.order_id, actions), dispatcher.dispatch(order.order_id, actions)
        )
        replay = await dispatcher.dispatch(order.order_id, actions)
        service_calls = []

        class TestService:
            async def fetch(self, request):
                body = await request.json()
                if hasattr(body, "to_py"):
                    body = body.to_py()
                service_calls.append((body["order_id"], body["workflow_key"], body["config_digest"]))

                async def response_json():
                    return {"provider_reference": "test-service-receipt"}

                return SimpleNamespace(status=200, json=response_json)

        test_env = SimpleNamespace(
            DB=self.env.DB,
            CARTRAY_ENVIRONMENT="test",
            CARTRAY_POST_PURCHASE_MODE="test",
            CARTRAY_POST_PURCHASE_TOKEN="t" * 32,
            POST_PURCHASE_HANDLER=TestService(),
            CARTRAY_POST_PURCHASE_CONFIG=json.dumps(
                {
                    "merchant": "smoke-store",
                    "environment": "test",
                    "actions": [
                        {
                            "workflow_key": "workflow-three",
                            "offer_keys": ["SMOKE-TEMPLATE"],
                            "handler": "service",
                            "config_ref": "test/three",
                            "config_digest": "c" * 64,
                        }
                    ],
                }
            ),
        )
        await _drain_post_purchase(test_env)
        await _drain_post_purchase(test_env)
        workflow_receipts = (
            await self.env.DB.prepare(
                "SELECT workflow_key,state FROM post_purchase_executions WHERE order_id=? ORDER BY workflow_key"
            )
            .bind(order.order_id)
            .all()
        )
        workflow_rows = _results(workflow_receipts)
        workflows_once = (
            sorted(calls)
            == [
                (order.order_id, "workflow-one"),
                (order.order_id, "workflow-two"),
            ]
            and replay == {"workflow-one": "already_claimed", "workflow-two": "already_claimed"}
            and service_calls == [(order.order_id, "workflow-three", "c" * 64)]
            and len(workflow_rows) == 3
            and all(row["state"] == "accepted" for row in workflow_rows)
            and all(set(result) == {"workflow-one", "workflow-two"} for result in runs)
        )

        budget_actions = tuple(
            Action(f"budget-{n}", ("SMOKE-TEMPLATE",), "email", f"test/{n}", "d" * 64)
            for n in range(100)
        )
        budget_outcomes = await dispatcher.dispatch(
            order.order_id, budget_actions, max_new_claims=2, deadline=time.monotonic() + 5
        )
        budget_deferred = len(budget_outcomes) == 100 and all(value == "deferred" for value in budget_outcomes.values())

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
        result = {
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
        }
        expected = {
            "d1_batch_owner": True,
            "first_confirmation": True,
            "lease_rejected": True,
            "outbox_events": ["OrderCreated", "CheckoutRedirectIssued", "OrderConfirmed"],
            "redirect_persisted": True,
            "signature_verified": True,
            "verifier_accepted": True,
            "workflows_once": True,
            "budget_deferred": True,
        }
        return Response.json(result, status=200 if result == expected else 500)
