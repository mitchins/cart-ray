"""Local-only Workerd smoke target for CartRay's D1 and Web Crypto boundaries."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from types import SimpleNamespace

from js import Request, crypto
from kinglet.storage import bytes_to_arraybuffer
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from cartray.errors import CheckoutInProgress
from cartray.fulfilment import (
    ProviderRegistry,
    Redirect,
    Stream,
    authorised_access,
    checkout_plan,
    external_link,
    mark_notification_sending,
    prepare_notification,
    private_object,
    recover_prepared_notification,
    valid_external_config,
    valid_private_config,
    validate_provider_resources,
)
from cartray.models import CheckoutOrder, CheckoutRedirect, OrderItem
from cartray.post_purchase import Action, D1Dispatcher
from cartray.store import D1OrderStore, _results
from cartray.worker import _drain_post_purchase, create_app
from cartray.workers_crypto import WorkersEd25519Signer, WorkersEd25519Verifier

TEST_PRIVATE_KEY_PKCS8_B64 = "MC4CAQAwBQYDK2VwBCIEIJZ4orlcGRX4w6SOrvHeEh5ZpFSC0FR4Onu1OKF/Lt87"
TEST_PUBLIC_KEY_RAW_B64 = "mjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PUBLIC_KEY_SPKI_B64 = "MCowBQYDK2VwAyEAmjCIMxYDlWSJsqHfrQZ+nVkoUEXEiajZ00z2jNxrijg="
TEST_PAYLOAD = b"cartray-worker-webcrypto-smoke-v1"


async def fulfilment_smoke(env, suffix):
    import hashlib

    object_key = "object-" + suffix
    await env.FULFILMENT_TEST_BUCKET.put(object_key, "dummy object")
    configs = ({"url": "https://example.test/file", "allowed_hosts": ["example.test"]},
               {"bucket_binding": "FULFILMENT_TEST_BUCKET", "key": object_key}, {})
    names = ("link-" + suffix, "object-" + suffix, "third-" + suffix)
    for index, provider in enumerate(("external_link", "private_object", "third")):
        encoded = json.dumps(configs[index], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        await env.DB.prepare("INSERT INTO fulfilment_resources VALUES (?,?,?,?,?,?,?,?,?,?)").bind(
            "smoke-store", "test", names[index], "v1", provider, names[index],
            hashlib.sha256(encoded.encode()).hexdigest(), "dummy.txt", "text/plain", '{"expires_at":null}',
        ).run()
    action_doc = {"merchant": "smoke-store", "environment": "test", "actions": [{
        "workflow_key": "deliver", "offer_keys": ["FULFILMENT"], "handler": "service",
        "config_ref": "test/v1", "config_digest": "a" * 64,
    }]}
    plan_doc = {"actions": action_doc, "no_delivery_offers": [], "bindings": [
        {"workflow_key": "deliver", "product_key": "FULFILMENT",
        "resources": [{"id": name, "version": "v1"} for name in names]}]}
    item = OrderItem("FULFILMENT", 1, "price_smoke", 1, tuple(name + "@v1" for name in names))
    plan, bindings = checkout_plan(json.dumps(plan_doc), (item,))
    order = CheckoutOrder("cr_fulfil_" + suffix, "fulfil_" + suffix, "fp", "sha256:" + "0" * 64,
                          (item,), "sha256:" + "2" * 64, "aud", 1,
                          fulfilment_plan=plan, fulfilment_bindings=bindings)
    store = D1OrderStore(env.DB)
    refs = sorted({(entry.resource_id, entry.resource_version) for entry in bindings})
    rows = await store.fulfilment_resource_rows("smoke-store", "test", refs)
    provider_config = json.dumps({"merchant": "smoke-store", "resources":
                                  {names[i]: configs[i] for i in range(3)}})
    validate_provider_resources(plan, bindings, rows, provider_config, int(time.time()),
        {"external_link": valid_external_config, "private_object": valid_private_config,
         "third": lambda config, _env: config == {}}, env)
    await store.start_or_load(order, nonce="nonce", now=10)
    session = "cs_fulfil_" + suffix
    await store.attach_redirect(order.order_id, CheckoutRedirect(session, "https://checkout.stripe.test/x"), now=11)
    await store.confirm_settlement(order_id=order.order_id, session_id=session, event_id="evt_fulfil_" + suffix,
                                   payload={"id": "evt_fulfil_" + suffix}, payload_sha256="sha256:test",
                                   payment_status="paid", amount_total_minor=1, now=12)
    calls = []
    async def handler(_purchase, execution):
        calls.append(execution.access_links)
        return "receipt"
    dispatcher = D1Dispatcher(env.DB, "smoke-store", "test", {"service": handler}, pinned=True,
                              fulfilment_origin="https://store.example.test")
    action = Action("deliver", ("FULFILMENT",), "service", "test/v1", "a" * 64)
    outcomes = await asyncio.gather(dispatcher.dispatch(order.order_id, (action,)),
                                    dispatcher.dispatch(order.order_id, (action,)))
    replay = await dispatcher.dispatch(order.order_id, (action,))
    async def third(resource, _config):
        return Stream("dummy", resource.filename, resource.mime_type)
    registry = ProviderRegistry({"external_link": external_link, "private_object": private_object, "third": third})
    settings = {("smoke-store", name): (configs[i], hashlib.sha256(json.dumps(
        configs[i], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())
        for i, name in enumerate(names)}
    settings[("smoke-store", names[1])] = ({"bucket": env.FULFILMENT_TEST_BUCKET, "key": object_key},
        settings[("smoke-store", names[1])][1])
    if len(calls) != 1 or len(calls[0]) != 3:
        return False
    values = [url.split("?token=", 1)[1] for _, url in calls[0]]
    resolved = [await authorised_access(env.DB, value, registry, settings, int(time.time())) for value in values]
    class Limiter:
        async def limit(self, _key):
            return {"success": True}
    endpoint_env = SimpleNamespace(DB=env.DB, CARTRAY_ENVIRONMENT="test", CARTRAY_FULFILMENT_MODE="test",
        CARTRAY_SIGNING_KEY_ID="smoke-installation", FULFILMENT_RATE_LIMITER=Limiter(),
        CARTRAY_FULFILMENT_PROVIDER_CONFIG=json.dumps({"merchant": "smoke-store", "resources":
                                                       {names[i]: configs[i] for i in range(3)}}),
        FULFILMENT_TEST_BUCKET=env.FULFILMENT_TEST_BUCKET)
    endpoint = create_app(fulfilment_registry=registry)
    options = to_js({"headers": {"cf-connecting-ip": "192.0.2.1"}}, dict_converter=__import__("js").Object.fromEntries)
    object_request = Request.new("https://store.example.test/fulfilment/access?token=" + values[1], options)
    third_request = Request.new("https://store.example.test/fulfilment/access?token=" + values[2], options)
    object_response = await endpoint(object_request, endpoint_env)
    third_response = await endpoint(third_request, endpoint_env)
    object_body = await object_response.text()
    third_body = await third_response.text()
    recovery_doc = json.loads(json.dumps(plan_doc))
    recovery_doc["actions"]["actions"][0]["workflow_key"] = "recover"
    recovery_doc["bindings"][0]["workflow_key"] = "recover"
    recovery_plan, recovery_bindings = checkout_plan(json.dumps(recovery_doc), (item,))
    recovery_order = replace(order, order_id="cr_recover_" + suffix, checkout_request_id="recover_" + suffix,
                             fulfilment_plan=recovery_plan, fulfilment_bindings=recovery_bindings)
    await store.start_or_load(recovery_order, nonce="nonce", now=10)
    recovery_session = "cs_recover_" + suffix
    await store.attach_redirect(recovery_order.order_id,
                                CheckoutRedirect(recovery_session, "https://checkout.stripe.test/x"), now=11)
    await store.confirm_settlement(order_id=recovery_order.order_id, session_id=recovery_session,
        event_id="evt_recover_" + suffix, payload={"id": "evt_recover_" + suffix}, payload_sha256="sha256:test",
        payment_status="paid", amount_total_minor=1, now=12)
    recovery_action = Action("recover", ("FULFILMENT",), "service", "test/v1", "a" * 64)
    old_nonce, old_tokens = await prepare_notification(env.DB, "smoke-store", "test", recovery_order.order_id,
                                                       recovery_action, 100)
    new_nonce, new_tokens = await recover_prepared_notification(env.DB, "smoke-store", "test", recovery_order.order_id,
        "recover", "test-operator", "verified no send began", 1300)
    try:
        await mark_notification_sending(env.DB, "smoke-store", "test", recovery_order.order_id, "recover", old_nonce)
        return False
    except ValueError:
        pass
    await mark_notification_sending(env.DB, "smoke-store", "test", recovery_order.order_id, "recover", new_nonce)
    try:
        await recover_prepared_notification(env.DB, "smoke-store", "test", recovery_order.order_id,
                                            "recover", "test-operator", "unknown receipt", 2600)
        return False
    except ValueError:
        pass
    if {entry[2] for entry in old_tokens} & {entry[2] for entry in new_tokens}:
        return False
    return (replay == {"deliver": "already_claimed"} and len(outcomes) == 2
            and isinstance(resolved[0], Redirect) and isinstance(resolved[1], Stream)
            and isinstance(resolved[2], Stream) and object_response.status == 200 and object_body == "dummy object"
            and third_response.status == 200 and third_body == "dummy")


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

        fulfilment_once = await fulfilment_smoke(self.env, token)
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
            "fulfilment_once": fulfilment_once,
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
            "fulfilment_once": True,
        }
        return Response.json(result, status=200 if result == expected else 500)
