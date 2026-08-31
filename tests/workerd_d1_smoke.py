"""Local-only Workerd smoke target for CartRay's D1 and Web Crypto boundaries."""

from __future__ import annotations

import base64
import time

from js import crypto
from kinglet.storage import bytes_to_arraybuffer
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from cartray.errors import CheckoutInProgress
from cartray.models import CheckoutOrder, CheckoutRedirect, OrderItem
from cartray.store import D1OrderStore, _results
from cartray.workers_crypto import WorkersEd25519Signer

TEST_PRIVATE_KEY_PKCS8_B64 = "MC4CAQAwBQYDK2VwBCIEIJZ4orlcGRX4w6SOrvHeEh5ZpFSC0FR4Onu1OKF/Lt87"
TEST_PUBLIC_KEY_RAW_B64 = "mjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PAYLOAD = b"cartray-worker-webcrypto-smoke-v1"


class Default(WorkerEntrypoint):
    async def fetch(self, _request):
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
        session_rows = _results(session)
        result = {
            "d1_batch_owner": first.owner,
            "lease_rejected": lease_rejected,
            "outbox_events": [row["event_type"] for row in _results(outbox)],
            "redirect_persisted": len(session_rows) == 1
            and session_rows[0]["external_session_id"] == redirect.session_id
            and session_rows[0]["redirect_url"] == redirect.url,
            "signature_verified": bool(signature_verified),
        }
        expected = {
            "d1_batch_owner": True,
            "lease_rejected": True,
            "outbox_events": ["OrderCreated", "CheckoutRedirectIssued"],
            "redirect_persisted": True,
            "signature_verified": True,
        }
        return Response.json(result, status=200 if result == expected else 500)
