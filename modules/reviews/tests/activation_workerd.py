"""Exercise no-backfill activation against independent local Workerd/D1 bindings."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from native_acquisition_workerd import AUTOMATION_TOKEN, ISSUER, internal, synthetic_settlement
from native_workerd import ADMIN
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
        manifest, _, _ = synthetic_settlement(seed)
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
                "REVIEWS_AUTOMATION_INGRESS_TOKEN": AUTOMATION_TOKEN,
                "REVIEWS_AUTOMATION_ACTIVATION_TOKEN": ACTIVATION_TOKEN,
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
                    "SELECT id FROM outbox WHERE event_type='OrderConfirmed' ORDER BY id LIMIT 1",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            outbox_id = json.loads(query.stdout)[0]["results"][0]["id"]
            assert (
                internal("/reviews-internal/v1/automation-intake", {"outbox_id": outbox_id}, AUTOMATION_TOKEN)[1][
                    "status"
                ]
                == "accepted"
            )
            print(
                "Workerd activation verified primary, authentication, immutable policy and subsequent order"
            )
        finally:
            stop(process)


if __name__ == "__main__":
    main()
