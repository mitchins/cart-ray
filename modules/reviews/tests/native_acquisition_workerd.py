"""Exercise acquisition-only collection against two disposable local D1 databases."""

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

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
        _spec, session, event = _checkout_session(
            checkout,
            checkout.gateway,
            CheckoutRequest(f"reviews-synthetic-{index}", catalogue.version, (CanonicalItem("TEST-TEMPLATE", 1),)),
            payment_status="paid",
            amount_total_minor=5000,
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
    assert len(inserts) == 8
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
        # Synthetic purpose-bound consent is provisioned independently from
        # CartRay settlement, as a trusted consent adapter would do.
        consent_sql = Path(directory) / "consent.sql"
        consent_sql.write_text(
            "\n".join(
                "INSERT INTO review_invitation_consents VALUES "
                f"('example-store','test','{order_id}','{recipient * 64}','review_invitation',"
                "'synthetic-consent',1,NULL);"
                for order_id, recipient in zip(order_ids, ("a", "b"), strict=True)
            )
        )
        subprocess.run(
            [wrangler, "d1", "execute", "cartray-reviews-local-test", *common, "--file", str(consent_sql)],
            cwd=ROOT,
            check=True,
        )
        process = start(
            "wrangler.acquisition.test.toml",
            directory,
            "verified_acquisition_only",
            ADMIN,
            {
                "REVIEWS_SETTLEMENT_TOKEN": ISSUER,
                "REVIEWS_INVITATION_KEY": "i" * 40,
                "REVIEWS_REVIEWER_KEY": "r" * 40,
                "REVIEWS_PUBLIC_STORE_NAME": "Example Store",
                "REVIEWS_SUBJECT_MAPS": json.dumps(
                    {manifest: {"TEST-TEMPLATE": "example-policy"}}, separators=(",", ":")
                ),
            },
        )
        try:
            subject = {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"}
            assert call("/reviews-admin/v1/subjects", subject, admin=True)[0] == 201
            assert (
                call(
                    "/reviews-admin/v1/subject-display-names",
                    {"subject_id": "example-policy", "public_name": "Example policy"},
                    admin=True,
                )[0]
                == 200
            )
            event = {"event_id": event_ids[0], "recipient_context_hash": "a" * 64}
            endpoint = "/reviews-internal/v1/settlements"
            assert internal(endpoint, event, ADMIN)[0] == 401
            status, invitation = internal(endpoint, event, ISSUER)
            assert status == 201, (status, invitation)
            assert internal(endpoint, event, ISSUER) == (status, invitation)
            landing = "/reviews/v1/invitation"
            capability = {"invitation_token": invitation["token"]}
            assert call(landing, capability) == (
                200,
                {
                    "store_name": "Example Store",
                    "subjects": [
                        {"subject_id": "example-policy", "public_name": "Example policy", "progress": "available"}
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
            assert call(landing, capability)[1]["subjects"][0]["progress"] == "submitted"
            assert call("/reviews/v1/contributions", body, key="other-request")[0] == 409
            listing = "/reviews/v1/subjects/example-policy/contributions"
            assert call(listing)[1]["contributions"] == []
            action = "/reviews-admin/v1/contributions/" + pending["review_id"] + "/approve"
            assert internal(action, {"revision_id": pending["revision_id"]}, ISSUER)[0] == 401
            assert call(action, {"revision_id": pending["revision_id"]}, admin=True)[0] == 200
            public = call(listing)[1]
            assert public["contributions"][0]["verification"] == "cartray_verified_acquisition"
            assert invitation["token"] not in json.dumps(public)
            revoke = "/reviews-internal/v1/invitations/revoke"
            assert internal(revoke, {"invitation_token": invitation["token"]}, ADMIN)[0] == 401
            assert internal(revoke, {"invitation_token": invitation["token"]}, ISSUER)[0] == 200
            assert call("/reviews/v1/contributions", body, key=winning_key)[0] == 409
            assert len(call(listing)[1]["contributions"]) == 1
            status, fresh = internal(endpoint, {"event_id": event_ids[1], "recipient_context_hash": "b" * 64}, ISSUER)
            assert status == 201
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
