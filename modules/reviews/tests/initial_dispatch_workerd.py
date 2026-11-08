"""Exercise no-backfill activation against independent local Workerd/D1 bindings."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from native_acquisition_workerd import (
    AUTOMATION_TOKEN,
    CONSENT_SECRET,
    ISSUER,
    internal,
    signed_consent,
    synthetic_settlement,
)
from native_workerd import ADMIN, BASE, call
from workerd_integration import MODULE, ROOT, start, stop

ACTIVATION_TOKEN = "synthetic-activation-token-0123456789"


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-activation-d1-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True)
        wrangler = str(ROOT / "node_modules/.bin/wrangler")
        common = ["--config", str(MODULE / "wrangler.acquisition.test.toml"), "--local", "--persist-to", directory]
        subprocess.run(
            [wrangler, "d1", "migrations", "apply", "cartray-acquisition-local-test", *common], cwd=ROOT, check=True
        )
        seed = Path(directory) / "synthetic.sql"
        manifest, events, orders = synthetic_settlement(seed)
        process = start(
            "wrangler.acquisition.test.toml",
            directory,
            "verified_acquisition_only",
            ADMIN,
            {
                "REVIEWS_SETTLEMENT_TOKEN": ISSUER,
                "REVIEWS_INVITATION_KEY": "i" * 40,
                "REVIEWS_REVIEWER_KEY": "r" * 40,
                "REVIEWS_AUTOMATION_INTAKE": "test",
                "REVIEWS_AUTOMATION_SENDING": "test",
                "REVIEWS_UNSUBSCRIBE_KEY": "u" * 40,
                "REVIEWS_CONSENT_SECRET": CONSENT_SECRET,
                "REVIEWS_AUTOMATION_INGRESS_TOKEN": AUTOMATION_TOKEN,
                "REVIEWS_AUTOMATION_ACTIVATION_TOKEN": ACTIVATION_TOKEN,
                "REVIEWS_RECIPIENT_EVIDENCE_SECRET": "p" * 40,
                "REVIEWS_SUBJECT_MAPS": json.dumps(
                    {manifest: {"TEST-TEMPLATE": "example-policy", "TEST-BUNDLE": "example-bundle"}}
                ),
            },
        )
        try:
            activation = "/reviews-internal/v1/automation-activate"
            policy = {"policy_version": "test-v1", "delay_seconds": 0, "max_lateness": 86400}
            assert internal(activation, policy, ISSUER)[0] == 401
            status, active = internal(activation, policy, ACTIVATION_TOKEN)
            assert status == 200 and active["status"] == "active" and active["outbox_watermark"] == 0
            assert internal(activation, policy, ACTIVATION_TOKEN) == (status, active)
            assert internal(activation, policy | {"policy_version": "other"}, ACTIVATION_TOKEN)[0] == 409
            subprocess.run(
                [wrangler, "d1", "execute", "cartray-acquisition-local-test", *common, "--file", str(seed)],
                cwd=ROOT,
                check=True,
            )
            query = subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-acquisition-local-test",
                    *common,
                    "--json",
                    "--command",
                    "SELECT id FROM outbox WHERE event_type='OrderConfirmed' ORDER BY id LIMIT 2",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            outbox_id = json.loads(query.stdout)[0]["results"][0]["id"]
            second_outbox = json.loads(query.stdout)[0]["results"][1]["id"]
            assert (
                internal("/reviews-internal/v1/automation-intake", {"outbox_id": outbox_id}, AUTOMATION_TOKEN)[1][
                    "status"
                ]
                == "accepted"
            )
            recipient = "a" * 64
            subprocess.run(
                ["node", str(MODULE / "consent/tests/automatic_real_reviews.mjs")],
                cwd=ROOT,
                env=os.environ
                | {
                    "ACORN_TEST_OUTBOX": str(outbox_id),
                    "ACORN_TEST_EVENT": events[0],
                    "ACORN_TEST_ORDER": orders[0],
                    "ACORN_TEST_REVIEWS_BASE": BASE,
                },
                check=True,
            )
            status, evidence = internal(
                "/reviews-internal/v1/automation-evidence-status", {"outbox_id": outbox_id}, AUTOMATION_TOKEN
            )
            assert status == 200 and evidence["recipient_binding_recorded"] and evidence["consent_granted"]
            for subject in ("example-policy", "example-bundle"):
                assert (
                    call(
                        "/reviews-admin/v1/subjects",
                        {"subject_id": subject, "kind": "product", "path": "/" + subject + "/"},
                        admin=True,
                    )[0]
                    == 201
                )
            endpoint = "/reviews-internal/v1/automation-initial-prepare"
            status, prepared = internal(endpoint, {"outbox_id": outbox_id}, AUTOMATION_TOKEN)
            assert status == 200 and prepared["state"] == "prepared", (status, prepared)
            assert internal(endpoint, {"outbox_id": outbox_id}, AUTOMATION_TOKEN)[1] == prepared
            assert (
                internal("/reviews-internal/v1/automation-intake", {"outbox_id": second_outbox}, AUTOMATION_TOKEN)[1][
                    "status"
                ]
                == "accepted"
            )
            subprocess.run(
                ["node", str(MODULE / "consent/tests/automatic_real_reviews.mjs")],
                cwd=ROOT,
                env=os.environ
                | {
                    "ACORN_TEST_OUTBOX": str(second_outbox),
                    "ACORN_TEST_EVENT": events[1],
                    "ACORN_TEST_ORDER": orders[1],
                    "ACORN_TEST_REVIEWS_BASE": BASE,
                },
                check=True,
            )
            second_status, second = internal(endpoint, {"outbox_id": second_outbox}, AUTOMATION_TOKEN)
            assert second_status == 200 and second["state"] == "prepared"
            token = prepared["unsubscribe_token"]
            assert call("/reviews/v1/invitation-unsubscribe", {"token": token})[0] == 200
            assert call("/reviews/v1/invitation-unsubscribe", {"token": token})[0] == 200
            assert internal(endpoint, {"outbox_id": outbox_id}, AUTOMATION_TOKEN) == (200, {"status": "suppressed"})
            assert internal(endpoint, {"outbox_id": second_outbox}, AUTOMATION_TOKEN) == (200, {"status": "suppressed"})
            second_claim = {
                "outbox_id": second_outbox,
                "acquisition_id": second["acquisition_id"],
                "generation_id": second["generation_id"],
                "delivery_identity": second["delivery_identity"],
            }
            assert internal("/reviews-internal/v1/automation-initial-claim", second_claim, AUTOMATION_TOKEN)[0] == 409
            claim = {
                "outbox_id": outbox_id,
                "acquisition_id": prepared["acquisition_id"],
                "generation_id": prepared["generation_id"],
                "delivery_identity": prepared["delivery_identity"],
            }
            assert internal("/reviews-internal/v1/automation-initial-claim", claim, AUTOMATION_TOKEN)[0] == 409
            # Exercise independently signed withdrawal on the other acquisition;
            # the first has already received an unsubscribe tombstone above.
            withdrawal = {
                "event_id": events[1],
                "recipient_context_hash": recipient,
                "evidence_reference": "synthetic-withdrawal",
                "withdrawn_at": int(time.time()),
            }
            canonical = json.dumps(
                ["reviews-consent-withdrawal-v1", "example-store", "test", withdrawal],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            signature = hmac.new(CONSENT_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
            request = urllib.request.Request(
                BASE + "/reviews-internal/v1/consents/withdraw",
                data=json.dumps(withdrawal).encode(),
                headers={"Content-Type": "application/json", "X-Reviews-Consent-Signature": signature},
                method="POST",
            )
            for _ in range(2):
                with urllib.request.urlopen(request, timeout=10) as response:
                    assert response.status == 201
            try:
                signed_consent(events[1], recipient)
                raise AssertionError("withdrawn consent was re-granted")
            except urllib.error.HTTPError as exc:
                assert exc.code == 409
            print("Workerd activation and purpose-bound withdrawal replay passed on two D1 databases")
            stop(process)
            process = start(
                "wrangler.acquisition.test.toml", directory, "disabled", ADMIN, {"REVIEWS_UNSUBSCRIBE_KEY": "u" * 40}
            )
            assert call("/reviews/v1/invitation-unsubscribe", {"token": token})[0] == 200
            print("Workerd initial due authorization and one-click suppression passed")
        finally:
            stop(process)


if __name__ == "__main__":
    main()
