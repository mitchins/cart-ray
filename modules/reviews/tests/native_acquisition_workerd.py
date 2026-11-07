"""Exercise acquisition-only collection against two disposable local D1 databases."""

import asyncio
import hashlib
import hmac
import json
import mailbox
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from native_workerd import ADMIN, BASE, call
from workerd_integration import MODULE, ROOT, start, stop

from cartray.canonical import CanonicalItem
from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver, build_catalogue_from_source
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.models import CheckoutRequest
from cartray.settlement import StripeSettlementService
from cartray.store import SqliteOrderStore
from cartray.stripe import CheckoutMetadataSealer, CheckoutMetadataVerifier

ISSUER = "synthetic-settlement-issuer-token-0123456789"
CONSENT_SECRET = "e" * 40
DELIVERY_TOKEN = "f" * 40
AUTOMATION_TOKEN = "automation-synthetic-issuer-token-0123456789"
RECIPIENT_SECRET = "recipient-synthetic-secret-0123456789"


def synthetic_settlement(sql_path):
    """Create settled synthetic facts using the actual CartRay settlement service."""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_settlement import StaticRetriever, _checkout_session

    async def create():
        fixture = ROOT / "fixtures"
        resolver = FixturePriceResolver.from_json(fixture / "price-resolutions.json")
        expansions = {k: tuple(v) for k, v in json.loads((fixture / "fulfilment-expansions.json").read_text()).items()}
        return await build_catalogue_from_source(
            CsvCatalogueSourceAdapter(fixture / "catalogue.csv"), resolver, expansions
        )

    class SyntheticSignature:
        async def sign(self, payload):
            return hmac.new(b"synthetic-settlement-signing-key", payload, hashlib.sha512).digest()

        async def verify(self, payload, signature):
            return hmac.compare_digest(await self.sign(payload), signature)

    catalogue = asyncio.run(create())
    checkout = CheckoutService(catalogue, SqliteOrderStore.in_memory(), FakePaymentGateway())
    signature = SyntheticSignature()
    events, orders = [], []
    for index in range(2):
        items = (CanonicalItem("TEST-TEMPLATE", 1),)
        if index == 0:
            items += (CanonicalItem("TEST-BUNDLE", 1),)
        _spec, session, event = _checkout_session(
            checkout,
            checkout.gateway,
            CheckoutRequest(f"reviews-synthetic-{index}", catalogue.version, items),
            payment_status="paid",
            amount_total_minor=7500 if index == 0 else 5000,
        )
        metadata = asyncio.run(
            CheckoutMetadataSealer("test", "synthetic", signature).seal(
                session_id=session.session_id, metadata=session.metadata
            )
        )
        session = replace(session, metadata=metadata)
        service = StripeSettlementService(
            checkout.store, StaticRetriever(session), CheckoutMetadataVerifier("test", {"synthetic": signature})
        )
        assert asyncio.run(service.settle(event)) is False
        events.append(event.event_id)
        orders.append(_spec.order_id)
    dumps = list(checkout.store.connection.iterdump())
    tables = ("orders", "order_items", "stripe_events", "checkout_sessions")
    inserts = [line for table in tables for line in dumps if line.startswith(f'INSERT INTO "{table}"')]
    assert len(inserts) == 9
    confirmed_outbox = [
        line for line in dumps if line.startswith('INSERT INTO "outbox"') and "'OrderConfirmed'" in line
    ]
    assert len(confirmed_outbox) == 2
    inserts.extend(confirmed_outbox)
    sql_path.write_text("\n".join(inserts) + "\n")
    return catalogue.version, events, orders


def internal(path, payload, token):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def signed_consent(event_id, recipient_hash):
    payload = {
        "event_id": event_id,
        "recipient_context_hash": recipient_hash,
        "evidence_reference": "synthetic-consent",
        "granted_at": 1,
    }
    signed = json.dumps(
        ["reviews-consent-v1", "example-store", "test", payload],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(CONSENT_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        BASE + "/reviews-internal/v1/consents",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Reviews-Consent-Signature": signature},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def signed_recipient(data, secret=RECIPIENT_SECRET):
    signed = json.dumps(
        ["reviews-automation-recipient-v1", "example-store", "test", data],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        BASE + "/reviews-internal/v1/automation-recipient-evidence",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "X-Reviews-Recipient-Signature": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-acquisition-d1-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True)
        config = str(MODULE / "wrangler.acquisition.test.toml")
        wrangler = str(ROOT / "node_modules/.bin/wrangler")
        common = ["--config", config, "--local", "--persist-to", directory]
        subprocess.run(
            [wrangler, "d1", "migrations", "apply", "cartray-acquisition-local-test", *common], cwd=ROOT, check=True
        )
        sql = Path(directory) / "synthetic.sql"
        manifest, event_ids, order_ids = synthetic_settlement(sql)
        subprocess.run(
            [wrangler, "d1", "execute", "cartray-acquisition-local-test", *common, "--file", str(sql)],
            cwd=ROOT,
            check=True,
        )
        outbox_result = subprocess.run(
            [
                wrangler,
                "d1",
                "execute",
                "cartray-acquisition-local-test",
                *common,
                "--json",
                "--command",
                "SELECT id,order_id FROM outbox WHERE event_type='OrderConfirmed' ORDER BY id LIMIT 1",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        outbox_id = json.loads(outbox_result.stdout)[0]["results"][0]["id"]
        assert json.loads(outbox_result.stdout)[0]["results"][0]["order_id"] == order_ids[0]
        subprocess.run(
            [
                wrangler,
                "d1",
                "execute",
                "cartray-reviews-local-test",
                *common,
                "--command",
                "INSERT INTO review_automation_activation VALUES('example-store','test',1,0,0,86400,'test-v1')",
            ],
            cwd=ROOT,
            check=True,
        )
        recipients = ("buyer@example.test", "second@example.test")
        trusted_recipients = dict(zip(order_ids, recipients, strict=True))
        recipient_hashes = [hashlib.sha256(recipient.encode()).hexdigest() for recipient in recipients]
        process = start(
            "wrangler.acquisition.test.toml",
            directory,
            "verified_acquisition_only",
            ADMIN,
            {
                "REVIEWS_SETTLEMENT_TOKEN": ISSUER,
                "REVIEWS_INVITATION_KEY": "i" * 40,
                "REVIEWS_REVIEWER_KEY": "r" * 40,
                "REVIEWS_CONSENT_SECRET": CONSENT_SECRET,
                "REVIEWS_DELIVERY_TOKEN": DELIVERY_TOKEN,
                "REVIEWS_AUTOMATION_INTAKE": "test",
                "REVIEWS_AUTOMATION_INGRESS_TOKEN": AUTOMATION_TOKEN,
                "REVIEWS_RECIPIENT_EVIDENCE_SECRET": RECIPIENT_SECRET,
                "REVIEWS_PUBLIC_STORE_NAME": "Example Store",
                "REVIEWS_RECOVERY_MODE": "manual_test",
                "REVIEWS_SUBJECT_MAPS": json.dumps(
                    {manifest: {"TEST-TEMPLATE": "example-policy", "TEST-BUNDLE": "example-bundle"}},
                    separators=(",", ":"),
                ),
            },
        )
        try:
            intake_path = "/reviews-internal/v1/automation-intake"
            assert internal(intake_path, {"outbox_id": outbox_id}, ISSUER)[0] == 401
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(lambda _: internal(intake_path, {"outbox_id": outbox_id}, AUTOMATION_TOKEN), range(4))
                )
            assert all(status == 200 and body["status"] == "accepted" for status, body in results)
            intake_counts = subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-reviews-local-test",
                    *common,
                    "--json",
                    "--command",
                    "SELECT (SELECT COUNT(*) FROM review_automation_intake) AS acknowledgements, "
                    "(SELECT COUNT(*) FROM review_automation_intents "
                    "WHERE state='held_eligibility_recipient_consent') AS intents, "
                    "(SELECT COUNT(*) FROM invitation_generations) AS generations",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(intake_counts.stdout)[0]["results"][0] == {
                "acknowledgements": 1,
                "intents": 1,
                "generations": 0,
            }
            checkout_ack = subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-acquisition-local-test",
                    *common,
                    "--json",
                    "--command",
                    f"SELECT delivered_at FROM outbox WHERE id={outbox_id}",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(checkout_ack.stdout)[0]["results"][0]["delivered_at"] is None
            due_path = "/reviews-internal/v1/automation-due-evaluate"
            due_data = {"acquisition_id": order_ids[0], "claim_token": "a" * 64}
            assert internal(due_path, due_data, ISSUER)[0] == 401
            assert internal(due_path, {"acquisition_id": "missing", "claim_token": "b" * 64}, AUTOMATION_TOKEN) == (
                200,
                {"status": "unavailable"},
            )
            with ThreadPoolExecutor(max_workers=8) as pool:
                due_results = list(pool.map(lambda _: internal(due_path, due_data, AUTOMATION_TOKEN), range(8)))
            assert all(result == (200, {"status": "held"}) for result in due_results)
            assert internal(due_path, due_data, AUTOMATION_TOKEN) == (200, {"status": "held"})
            held = subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-reviews-local-test",
                    *common,
                    "--json",
                    "--command",
                    "SELECT state FROM review_automation_intents",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(held.stdout)[0]["results"][0]["state"] == "held_eligibility_recipient_consent"
            recipient_data = {
                "outbox_id": outbox_id,
                "recipient_context_hash": recipient_hashes[0],
                "binding_version": "synthetic-v1",
                "evidence_reference": "synthetic-binding",
                "verified_at": 1,
            }
            assert signed_recipient(recipient_data, "wrong-synthetic-recipient-secret-012345")[0] == 409
            assert signed_recipient(recipient_data) == (200, {"status": "recorded"})
            assert signed_recipient(recipient_data) == (200, {"status": "recorded"})
            status_path = "/reviews-internal/v1/automation-evidence-status"
            assert internal(status_path, {"outbox_id": outbox_id}, ISSUER)[0] == 401
            status, evidence = internal(status_path, {"outbox_id": outbox_id}, AUTOMATION_TOKEN)
            assert status == 200 and evidence == {
                "state": "held_eligibility_recipient_consent",
                "recipient_binding_recorded": True,
                "consent_granted": False,
            }
            subject = {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"}
            assert call("/reviews-admin/v1/subjects", subject, admin=True)[0] == 201
            assert (
                call(
                    "/reviews-admin/v1/subjects",
                    {"subject_id": "example-bundle", "kind": "product", "path": "/example-bundle/"},
                    admin=True,
                )[0]
                == 201
            )
            assert (
                call(
                    "/reviews-admin/v1/subject-display-names",
                    {"subject_id": "example-bundle", "public_name": "Example bundle"},
                    admin=True,
                )[0]
                == 200
            )
            assert (
                call(
                    "/reviews-admin/v1/subject-display-names",
                    {"subject_id": "example-policy", "public_name": "Example policy"},
                    admin=True,
                )[0]
                == 200
            )
            event = {
                "event_id": event_ids[0],
                "recipient_context_hash": recipient_hashes[0],
                "request_key": "initial-1",
            }
            endpoint = "/reviews-internal/v1/settlements"
            assert internal(endpoint, event, ADMIN)[0] == 401
            assert internal(endpoint, event, ISSUER)[0] == 409
            counts = subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-reviews-local-test",
                    *common,
                    "--json",
                    "--command",
                    "SELECT COUNT(*) AS count FROM acquisition_invitations",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(counts.stdout)[0]["results"][0]["count"] == 0
            assert signed_consent(event_ids[0], recipient_hashes[0]) == 201
            assert signed_consent(event_ids[1], recipient_hashes[1]) == 201
            status, evidence = internal(status_path, {"outbox_id": outbox_id}, AUTOMATION_TOKEN)
            assert status == 200 and evidence == {
                "state": "held_eligibility_recipient_consent",
                "recipient_binding_recorded": True,
                "consent_granted": True,
            }
            status, invitation = internal(endpoint, event, ISSUER)
            assert status == 201, (status, invitation)
            assert internal(endpoint, event, ISSUER) == (status, invitation)
            landing = "/reviews/v1/invitation"
            capability = {"invitation_token": invitation["token"]}
            assert call(landing, capability)[0] == 404
            sys.path.insert(0, str(MODULE / "tools"))
            from test_mailbox import deliver_bound_invitation

            mailbox_path = Path(directory) / "synthetic-mail"
            first_claim = {
                "acquisition_id": invitation["acquisition_id"],
                "generation_id": invitation["generation_id"],
                "delivery_identity": invitation["delivery_identity"],
            }
            assert internal("/reviews-internal/v1/invitations/delivery-claim", first_claim, DELIVERY_TOKEN)[0] == 200
            message_id = deliver_bound_invitation(
                mailbox_path,
                acquisition_id=invitation["acquisition_id"],
                expected_recipient_hash=recipient_hashes[0],
                resolve_recipient=trusted_recipients.get,
                store_name="Example Store",
                landing_url="http://127.0.0.1:3000/review/",
                token=invitation["token"],
            )
            email = mailbox.Maildir(mailbox_path, create=False)[message_id].get_payload(decode=True).decode()
            url = next(line for line in email.splitlines() if line.startswith("http://"))
            emailed_token = parse_qs(urlsplit(url).fragment)["invitation"][0]
            assert emailed_token == invitation["token"]
            delivery = {
                "acquisition_id": invitation["acquisition_id"],
                "generation_id": invitation["generation_id"],
                "delivery_identity": invitation["delivery_identity"],
                "outcome": "accepted",
                "accepted_at": int(time.time()),
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(
                    pool.map(
                        lambda _: internal(
                            "/reviews-internal/v1/invitations/delivery-outcome", delivery, DELIVERY_TOKEN
                        ),
                        range(2),
                    )
                )
            assert outcomes == [(200, {"state": "accepted"}), (200, {"state": "accepted"})], outcomes
            capability = {"invitation_token": emailed_token}
            draft_scope = hmac.new(
                ("r" * 40).encode(),
                json.dumps(
                    ["reviews-draft-v1", "example-store", "test", invitation["acquisition_id"]], separators=(",", ":")
                ).encode(),
                hashlib.sha256,
            ).hexdigest()
            assert call(landing, capability) == (
                200,
                {
                    "store_name": "Example Store",
                    "draft_scope": draft_scope,
                    "subjects": [
                        {"subject_id": "example-bundle", "public_name": "Example bundle", "progress": "available"},
                        {"subject_id": "example-policy", "public_name": "Example policy", "progress": "available"},
                    ],
                },
            )
            assert call("/reviews-admin/v1/test-capabilities", {}, admin=True)[0] == 404
            assert call(endpoint)[0] == 404
            try:
                urllib.request.urlopen(urllib.request.Request(BASE + endpoint, method="HEAD"), timeout=10)
                raise AssertionError("HEAD unexpectedly accepted")
            except urllib.error.HTTPError as exc:
                assert exc.code == 405
            body = {
                "subject_id": "example-policy",
                "author": "Guest",
                "body": "Synthetic purchase review",
                "rating": 5,
                "invitation_token": invitation["token"],
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(
                    pool.map(
                        lambda key: (key, call("/reviews/v1/contributions", body, key=key)),
                        ("purchase-request-a", "purchase-request-b"),
                    )
                )
            assert sorted(result[0] for _, result in outcomes) == [202, 409], outcomes
            winning_key, (status, pending) = next(item for item in outcomes if item[1][0] == 202)
            assert call("/reviews/v1/contributions", body, key=winning_key) == (status, pending)
            assert [s["progress"] for s in call(landing, capability)[1]["subjects"]] == ["available", "submitted"]
            assert call("/reviews/v1/contributions", body, key="other-request")[0] == 409
            listing = "/reviews/v1/subjects/example-policy/contributions"
            assert call(listing)[1]["contributions"] == []
            action = "/reviews-admin/v1/contributions/" + pending["review_id"] + "/approve"
            assert internal(action, {"revision_id": pending["revision_id"]}, ISSUER)[0] == 401
            assert call(action, {"revision_id": pending["revision_id"]}, admin=True)[0] == 200
            public = call(listing)[1]
            assert public["contributions"][0]["verification"] == "cartray_verified_acquisition"
            assert invitation["token"] not in json.dumps(public)
            subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-reviews-local-test",
                    *common,
                    "--command",
                    "UPDATE acquisition_invitations SET expires_at=unixepoch()-1 "
                    f"WHERE source_event_id='{event_ids[0]}'",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            assert call(landing, capability)[0] == 404
            assert call("/reviews/v1/invitation-recovery", capability) == (202, {"status": "requested"})
            queued = internal("/reviews-internal/v1/invitations/recovery-next", {}, ISSUER)[1]["pending"]
            assert queued["acquisition_id"] == invitation["acquisition_id"]
            previous_messages = set(mailbox.Maildir(mailbox_path, create=False).keys())
            from process_recovery import process_one

            def delivery_post(path, payload, credential):
                result_status, result = internal(path, payload, credential)
                if result_status >= 400:
                    raise ValueError("delivery unavailable")
                return result

            assert process_one(
                delivery_post,
                issuer=ISSUER,
                delivery_token=DELIVERY_TOKEN,
                resolve_recipient=trusted_recipients.get,
                mailbox_path=mailbox_path,
                store_name="Example Store",
                landing_url="http://127.0.0.1:3000/review/",
            ) == {"status": "accepted"}
            inbox = mailbox.Maildir(mailbox_path, create=False)
            new_messages = set(inbox.keys()) - previous_messages
            assert len(new_messages) == 1
            replacement_email = inbox[new_messages.pop()].get_payload(decode=True).decode()
            replacement_url = next(line for line in replacement_email.splitlines() if line.startswith("http://"))
            replacement_token = parse_qs(urlsplit(replacement_url).fragment)["invitation"][0]
            assert internal("/reviews-internal/v1/invitations/recovery-next", {}, ISSUER)[1]["pending"] is None
            assert (
                internal(
                    "/reviews-internal/v1/invitations/recovery-prepare",
                    {"event_id": event_ids[0], "request_id": queued["request_id"]},
                    ISSUER,
                )[0]
                == 409
            )
            assert call(landing, capability)[0] == 404
            capability = {"invitation_token": replacement_token}
            assert call(landing, capability)[1]["draft_scope"] == draft_scope
            assert [s["progress"] for s in call(landing, capability)[1]["subjects"]] == ["available", "submitted"]
            assert call("/reviews/v1/contributions", body | capability, key=winning_key) == (status, pending)
            second_body = {"subject_id": "example-bundle", "rating": 2, **capability}
            status, second = call("/reviews/v1/contributions", second_body, key="second-product")
            assert status == 202
            assert [s["progress"] for s in call(landing, capability)[1]["subjects"]] == ["submitted", "submitted"]
            second_action = "/reviews-admin/v1/contributions/" + second["review_id"] + "/approve"
            assert call(second_action, {"revision_id": second["revision_id"]}, admin=True)[0] == 200
            assert call("/reviews/v1/subjects/example-bundle/summary")[1]["rating"] == {"average": 2, "rated_count": 1}
            management = "/reviews/v1/manage/" + second["review_id"] + "/"
            management_capability = second["management_token"]
            assert call(management + "read", {"management_token": management_capability})[1]["rating"] == 2
            edit = {
                "management_token": management_capability,
                "revision_id": second["revision_id"],
                "update": {"body": "Updated synthetic feedback", "rating": None},
            }
            edit_status, edited = call(management + "edit", edit, key="edit-second")
            assert edit_status == 200 and edited["status"] == "pending"
            assert call(management + "edit", edit, key="edit-second") == (edit_status, edited)
            assert call("/reviews/v1/subjects/example-bundle/summary")[1]["rating"]["rated_count"] == 0
            assert call(second_action, {"revision_id": edited["revision_id"]}, admin=True)[0] == 200
            assert call("/reviews/v1/subjects/example-bundle/summary")[1]["contribution_count"] == 1
            withdrawal = {"management_token": management_capability, "revision_id": edited["revision_id"]}
            assert call(management + "withdraw", withdrawal, key="withdraw-second")[1]["status"] == "withdrawn"
            assert call(management + "read", {"management_token": management_capability})[0] == 409
            assert call("/reviews/v1/subjects/example-bundle/summary")[1]["contribution_count"] == 0
            revoke = "/reviews-internal/v1/invitations/revoke"
            assert internal(revoke, capability, ADMIN)[0] == 401
            assert internal(revoke, capability, ISSUER)[0] == 200
            assert call("/reviews/v1/contributions", body | capability, key=winning_key)[0] == 409
            assert len(call(listing)[1]["contributions"]) == 1
            status, fresh = internal(
                endpoint,
                {"event_id": event_ids[1], "recipient_context_hash": recipient_hashes[1], "request_key": "initial-2"},
                ISSUER,
            )
            assert status == 201
            second_claim = {
                "acquisition_id": fresh["acquisition_id"],
                "generation_id": fresh["generation_id"],
                "delivery_identity": fresh["delivery_identity"],
            }
            assert internal("/reviews-internal/v1/invitations/delivery-claim", second_claim, DELIVERY_TOKEN)[0] == 200
            deliver_bound_invitation(
                mailbox_path,
                acquisition_id=fresh["acquisition_id"],
                expected_recipient_hash=recipient_hashes[1],
                resolve_recipient=trusted_recipients.get,
                store_name="Example Store",
                landing_url="http://127.0.0.1:3000/review/",
                token=fresh["token"],
            )
            second_delivery = {
                "acquisition_id": fresh["acquisition_id"],
                "generation_id": fresh["generation_id"],
                "delivery_identity": fresh["delivery_identity"],
                "outcome": "accepted",
                "accepted_at": int(time.time()),
            }
            assert (
                internal("/reviews-internal/v1/invitations/delivery-outcome", second_delivery, DELIVERY_TOKEN)[0] == 200
            )
            fresh_body = body | {"invitation_token": fresh["token"], "body": "Racing review"}
            with ThreadPoolExecutor(max_workers=2) as pool:
                revoke_future = pool.submit(internal, revoke, {"invitation_token": fresh["token"]}, ISSUER)
                submit_future = pool.submit(call, "/reviews/v1/contributions", fresh_body, key="race-request")
                revoked, submitted = revoke_future.result(), submit_future.result()
            assert revoked[0] == 200 and submitted[0] in (202, 409), (revoked, submitted)
            pending_queue = call("/reviews-admin/v1/pending", admin=True)[1]["pending"]
            assert sum(item["body"] == "Racing review" for item in pending_queue) == (submitted[0] == 202)
            assert call("/reviews/v1/contributions", fresh_body, key="race-retry")[0] == 409
            race_query = (
                "SELECT (SELECT COUNT(*) FROM native_review_acquisitions a "
                "JOIN acquisition_invitations i USING(store_key,environment,acquisition_id) "
                f"WHERE i.source_event_id='{event_ids[1]}') evidence, "
                "(SELECT COUNT(*) FROM acquisition_invitation_subjects s JOIN acquisition_invitations i "
                "USING(store_key,environment,acquisition_id) "
                f"WHERE i.source_event_id='{event_ids[1]}' AND s.consumed_at IS NOT NULL) consumed, "
                "(SELECT COUNT(*) FROM acquisition_invitations "
                f"WHERE source_event_id='{event_ids[1]}' AND revoked_at IS NOT NULL) revoked"
            )
            result = subprocess.run(
                [wrangler, "d1", "execute", "cartray-reviews-local-test", *common, "--json", "--command", race_query],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            counts = json.loads(result.stdout)[0]["results"][0]
            assert counts == {"evidence": int(submitted[0] == 202), "consumed": int(submitted[0] == 202), "revoked": 1}
            print("Synthetic confirmed purchase → invitation → pending → approved verified acquisition", flush=True)
        finally:
            stop(process)


if __name__ == "__main__":
    main()
