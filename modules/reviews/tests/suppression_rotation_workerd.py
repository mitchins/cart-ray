"""Qualify a frozen key rotation with three independent local Workerd D1 databases."""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from native_acquisition_workerd import ISSUER, internal
from native_workerd import ADMIN, call
from workerd_integration import MODULE, ROOT, start, stop

CONFIG = "wrangler.suppression-rotation.test.toml"
TOKEN = "rotation-token-" + "t" * 40
OLD = "old-suppression-key-" + "a" * 40
NEW = "new-suppression-key-" + "b" * 40
UNSUB = "unsubscribe-key-" + "u" * 40
EMAIL = "synthetic@example.test"


def digest(key):
    data = json.dumps(["acorn-recipient-v2", "example-store", "test", EMAIL], separators=(",", ":"))
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def key_check(key, version):
    data = json.dumps(["reviews-suppression-key-check-v1", "example-store", "test", "install", version],
                      separators=(",", ":"))
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-rotation-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True)
        wrangler = str(ROOT / "node_modules/.bin/wrangler")
        common = ["--config", str(MODULE / CONFIG), "--local", "--persist-to", directory]
        subprocess.run([wrangler, "d1", "migrations", "apply", "cartray-acquisition-local-test", *common],
                       cwd=ROOT, check=True)
        subprocess.run([wrangler, "d1", "migrations", "apply", "cartray-suppression-context-local-test", *common],
                       cwd=ROOT, check=True)
        statements = Path(directory) / "seed.sql"
        statements.write_text(
            "INSERT INTO review_automation_intake VALUES "
            "('example-store','test',10,'order_1','" + "a" * 64 + "',100,110,'v1','accepted');\n"
            "INSERT INTO review_automation_intents VALUES "
            "('example-store','test','order_1',10,110,'v1','held_eligibility_recipient_consent');\n"
            "INSERT INTO review_automation_recipient_bindings VALUES "
            "('example-store','test','order_1',10,'" + digest(OLD) + "','v2','purchase_contact_10',100);\n"
            "INSERT INTO review_invitation_consents VALUES "
            "('example-store','test','order_1','" + digest(OLD) + "','review_invitation','grant',100,NULL);\n"
        )
        subprocess.run([wrangler, "d1", "execute", "cartray-reviews-local-test", *common, "--file", str(statements)],
                       cwd=ROOT, check=True)
        context = Path(directory) / "context.sql"
        context.write_text(
            "INSERT INTO review_suppression_key_registry VALUES "
            "('example-store','test','install','key1','" + key_check(OLD, "key1") + "');\n"
            "INSERT INTO review_suppression_key_current VALUES ('example-store','test','install','key1');\n"
        )
        subprocess.run([wrangler, "d1", "execute", "cartray-suppression-context-local-test", *common,
                        "--file", str(context)], cwd=ROOT, check=True)
        process = start(CONFIG, directory, "verified_acquisition_only", ADMIN, {
            "REVIEWS_SETTLEMENT_TOKEN": ISSUER, "REVIEWS_INVITATION_KEY": "i" * 40,
            "REVIEWS_REVIEWER_KEY": "r" * 40, "REVIEWS_UNSUBSCRIBE_KEY": UNSUB,
            "REVIEWS_AUTOMATION_INTAKE": "test", "REVIEWS_AUTOMATION_INGRESS_TOKEN": "m" * 40,
            "REVIEWS_SUPPRESSION_ROTATION_MODE": "test", "REVIEWS_SUPPRESSION_ROTATION_TOKEN": TOKEN,
        })
        try:
            path = "/reviews-internal/v1/suppression-key-rotation"
            scope = {"old": "key1", "target": "key2"}
            assert internal(path, scope | {"action": "freeze"}, "bad")[0] == 401
            assert internal(path, scope | {"action": "freeze"}, TOKEN)[0] == 200
            switch = scope | {"action": "switch", "installation": "install", "old_key": OLD, "new_key": NEW}
            assert internal(path, switch, TOKEN)[0] == 409
            backfill = scope | {"action": "backfill", "old_key": OLD, "new_key": NEW, "contacts": [EMAIL]}
            assert internal(path, backfill, TOKEN)[0] == 200
            assert internal(path, switch | {"new_key": "wrong-key-" + "x" * 40}, TOKEN)[0] == 409
            assert internal(path, switch, TOKEN)[0] == 200
            activation = scope | {"action": "activate", "installation": "install", "new_key": NEW}
            assert internal(path, activation, TOKEN)[0] == 200
            from cartray_reviews.automation_initial import unsubscribe_token
            token = unsubscribe_token(secret=UNSUB.encode(), store="example-store", environment="test", outbox_id=10)
            assert call("/reviews/v1/invitation-unsubscribe", {"token": token})[0] == 200
            query = subprocess.run([wrangler, "d1", "execute", "cartray-reviews-local-test", *common, "--json",
                "--command", "SELECT recipient_context_hash FROM review_recipient_suppressions"],
                cwd=ROOT, capture_output=True, text=True, check=True)
            values = {row["recipient_context_hash"] for row in json.loads(query.stdout)[0]["results"]}
            assert values == {digest(OLD), digest(NEW)}
            replay = {"outbox_id": 10, "candidate": digest(NEW)}
            replay_result = internal("/reviews-internal/v1/automation-recipient-replay", replay, "m" * 40)
            assert replay_result[1] == {"status": "suppressed"}
            print("Workerd three-D1 fenced key rotation and old-link withdrawal passed")
        finally:
            stop(process)


if __name__ == "__main__":
    main()
