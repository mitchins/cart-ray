"""Read-only assertions and sanitized evidence for the Acorn hosted trust test.

Use only with a dedicated test account and test databases. This script neither
initiates checkout nor enables delivery and never prints Stripe's customer data.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
import urllib.request
from pathlib import Path

ACCOUNT = "33623e6d0b4793842a1832c5512aeb49"
STRIPE_ACCOUNT = "acct_1UYzlzPheThyzcxa"
CATALOGUE = "sha256:4a37e90f0e4a792f5b2167b9a83a22abcc5f0bb4273bc5a9052c0ca2aa2d6c6f"
EXPECTED_ITEMS = {"ACORN-KIT": (1, 2400), "ACORN-PLANTER": (1, 1800)}
WRANGLER = Path(__file__).resolve().parents[3] / "node_modules/.bin/wrangler"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rows(config, query):
    env = os.environ | {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
    result = subprocess.run(
        [str(WRANGLER), "d1", "execute", "DB", "--remote", "--config", str(config), "--json", "--command", query],
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
        env=env,
    )
    data = json.loads(result.stdout)
    require(len(data) == 1 and data[0]["success"], "D1 query did not succeed")
    return data[0]["results"]


def deployed_version(config, expected_bindings):
    """Check the active deployed source version and actual D1 binding."""
    env = os.environ | {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT}

    def wrangler(*args):
        result = subprocess.run(
            [str(WRANGLER), *args, "--config", str(config), "--json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
            env=env,
        )
        return json.loads(result.stdout)

    deployments = wrangler("deployments", "list")
    require(bool(deployments), "no active source deployment")
    latest = max(deployments, key=lambda d: d["created_on"])
    versions = latest.get("versions", [])
    require(len(versions) == 1 and versions[0].get("percentage") == 100, "split source deployment")
    version_id = versions[0]["version_id"]
    version = wrangler("versions", "view", version_id)
    bindings = version.get("resources", {}).get("bindings", [])
    check_bindings(bindings, expected_bindings)
    return version_id


def check_bindings(bindings, expected):
    for name, kind, field, value in expected:
        matching = [x for x in bindings if x.get("name") == name and x.get("type") == kind]
        require(len(matching) == 1 and matching[0].get(field) == value, "deployed binding mismatch: " + name)
        if kind == "service":
            require(
                matching[0].get("environment") in (None, "") and matching[0].get("entrypoint") in (None, ""),
                "non-default service target: " + name,
            )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the sandbox bearer credential across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Stripe redirect rejected")


def stripe_get(key, path):
    req = urllib.request.Request(
        "https://api.stripe.com/v1/" + path,
        headers={
            "Authorization": "Bearer " + key,
            "Stripe-Version": "2025-09-30.clover",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
        body = response.read(1024 * 1024 + 1)
    require(len(body) <= 1024 * 1024, "Stripe response too large")
    return json.loads(body)


def validate(
    old_id,
    old_source,
    source,
    items,
    activation,
    checkpoint,
    old_intake,
    old_intent,
    intake,
    intent,
    session,
    lines,
    expected_checkpoint,
):
    """Reject missing, duplicate, mismatched, unfenced or non-held observations."""
    require(
        len(old_source) == len(source) == len(activation) == len(checkpoint) == len(intake) == len(intent) == 1,
        "missing or duplicate evidence",
    )
    order, fence, cursor, receipt, held = source[0], activation[0], checkpoint[0], intake[0], intent[0]
    require(fence["fence_mode"] == "outbox_snapshot", "activation fence mismatch")
    require(all(cursor.get(key) == value for key, value in expected_checkpoint.items()), "checkpoint identity mismatch")
    require(
        old_id <= fence["outbox_watermark"] == cursor["activation_watermark"] < order["outbox_id"],
        "old order is not fenced",
    )
    require(
        not old_intent and all(row["disposition"] == "pre_activation" for row in old_intake),
        "old order generated an eligible intent",
    )
    require(
        old_source[0]["delivered_at"] is None
        and order["delivered_at"] is None
        and cursor["last_outbox_id"] >= order["outbox_id"],
        "global or subscriber acknowledgement mismatch",
    )
    require(
        order["manifest_version"] == CATALOGUE and order["currency"] == "aud" and order["subtotal_minor"] == 4200,
        "order catalogue or total mismatch",
    )
    require(
        order["settlement_state"] == "confirmed"
        and order["stripe_payment_status"] == "paid"
        and order["stripe_amount_total_minor"] == 4200,
        "order is not settled",
    )
    require(
        len(items) == 2
        and {r["product_key"]: (r["quantity"], r["unit_amount_minor"]) for r in items} == EXPECTED_ITEMS,
        "order item mismatch",
    )
    require(
        receipt["disposition"] == "accepted" and held["state"] == "held_eligibility_recipient_consent",
        "intent is not held",
    )
    require(
        receipt["acquisition_id"] == held["acquisition_id"] == order["order_id"]
        and receipt["outbox_id"] == held["outbox_id"] == order["outbox_id"],
        "intake identity mismatch",
    )
    require(
        session.get("livemode") is False
        and session.get("status") == "complete"
        and session.get("payment_status") == "paid"
        and session.get("currency") == "aud"
        and session.get("amount_total") == 4200,
        "Stripe session is not a paid test purchase",
    )
    require(not lines.get("has_more") and len(lines.get("data", [])) == 2, "Stripe line items incomplete")
    expected = sorted((r["stripe_price_id"], r["quantity"], r["quantity"] * r["unit_amount_minor"]) for r in items)
    observed = sorted((r["price"]["id"], r["quantity"], r["amount_total"]) for r in lines["data"])
    require(observed == expected, "Stripe line items mismatch")
    return {
        "store": "acorn-store",
        "environment": "test",
        "stripe_account": STRIPE_ACCOUNT,
        "catalogue_version": CATALOGUE,
        "old_outbox_id": old_id,
        "activation_watermark": fence["outbox_watermark"],
        "new_outbox_id": order["outbox_id"],
        "order_id": order["order_id"],
        "stripe_session_id": session["id"],
        "amount_minor": 4200,
        "currency": "aud",
        "reviews_receipt_count": 1,
        "intent_state": held["state"],
        "global_outbox_delivered": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-config", required=True, type=Path)
    parser.add_argument("--reviews-config", required=True, type=Path)
    parser.add_argument("--producer-config", required=True, type=Path)
    parser.add_argument("--stripe-key-file", required=True, type=Path)
    parser.add_argument("--old-outbox-id", required=True, type=int)
    parser.add_argument("--new-outbox-id", required=True, type=int)
    args = parser.parse_args()
    require(0 < args.old_outbox_id < args.new_outbox_id, "invalid outbox ids")
    configs = [tomllib.loads(x.read_text()) for x in (args.source_config, args.reviews_config, args.producer_config)]
    require(all(x.get("account_id") == ACCOUNT for x in configs), "wrong Cloudflare account")

    def db_id(config, binding):
        found = [x["database_id"] for x in config.get("d1_databases", []) if x["binding"] == binding]
        require(len(found) == 1, "missing database binding")
        return found[0]

    source_id = db_id(configs[0], "DB")
    require(
        source_id != db_id(configs[1], "REVIEWS_DB") and source_id == db_id(configs[1], "CARTRAY_DB"),
        "database isolation mismatch",
    )
    reviews_id = db_id(configs[1], "REVIEWS_DB")
    require(db_id(configs[2], "COMMERCE_DB") == source_id, "producer database mismatch")
    variables = configs[2]["vars"]
    require(
        variables["SOURCE_DATABASE_ID"] == source_id
        and variables["STRIPE_ACCOUNT_ID"] == STRIPE_ACCOUNT
        and variables["CATALOGUE_VERSION"] == CATALOGUE
        and variables["SOURCE_STORE"] == "acorn-store",
        "producer identity mismatch",
    )
    expected_checkpoint = dict(
        subscriber_id=variables["SOURCE_ISSUER"],
        installation_id=variables["SOURCE_INSTALLATION"],
        source_database_id=source_id,
        stripe_account_id=STRIPE_ACCOUNT,
        catalogue_version=CATALOGUE,
        mapping_sha256=variables["MAPPING_SHA256"],
    )
    require(
        hashlib.sha256(configs[1]["vars"]["REVIEWS_SUBJECT_MAPS"].encode()).hexdigest() == variables["MAPPING_SHA256"],
        "mapping digest mismatch",
    )
    source_bindings = [("DB", "d1", "database_id", source_id), ("CARTRAY_ENVIRONMENT", "plain_text", "text", "test")]
    reviews_bindings = [
        ("REVIEWS_DB", "d1", "database_id", reviews_id),
        ("CARTRAY_DB", "d1", "database_id", source_id),
        ("REVIEWS_ENVIRONMENT", "plain_text", "text", "test"),
        ("REVIEWS_STORE_KEY", "plain_text", "text", "acorn-store"),
    ]
    reviews_bindings.append(("REVIEWS_SUBJECT_MAPS", "plain_text", "text", configs[1]["vars"]["REVIEWS_SUBJECT_MAPS"]))
    producer_bindings = [
        ("COMMERCE_DB", "d1", "database_id", source_id),
        ("SOURCE_API", "service", "service", configs[0]["name"]),
        ("REVIEWS_API", "service", "service", configs[1]["name"]),
    ]
    producer_bindings.extend(
        (key, "plain_text", "text", variables[key])
        for key in (
            "SOURCE_ISSUER",
            "SOURCE_INSTALLATION",
            "SOURCE_DATABASE_ID",
            "STRIPE_ACCOUNT_ID",
            "CATALOGUE_VERSION",
            "MAPPING_SHA256",
        )
    )
    producer_version = deployed_version(args.producer_config, producer_bindings)
    active_version = deployed_version(args.source_config, source_bindings)
    reviews_version = deployed_version(args.reviews_config, reviews_bindings)
    # Wrangler DB alias is only local query configuration, never a mutation.
    reviews_text = args.reviews_config.read_text().replace('binding = "REVIEWS_DB"', 'binding = "DB"', 1)
    require('binding = "DB"' in reviews_text, "reviews binding unavailable")
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        rc = Path(temp) / "reviews-query.toml"
        rc.write_text(reviews_text)
        where = "store_key='acorn-store' AND environment='test'"
        old, new = args.old_outbox_id, args.new_outbox_id
        query = lambda q: rows(rc, q)  # noqa: E731
        old_source = rows(
            args.source_config, f"SELECT delivered_at FROM outbox WHERE id={old} AND event_type='OrderConfirmed'"
        )
        source = rows(
            args.source_config,
            "SELECT b.id outbox_id,b.delivered_at,o.order_id,o.manifest_version,o.currency,o.subtotal_minor,"
            "s.external_session_id,s.settlement_state,s.stripe_payment_status,s.stripe_amount_total_minor "
            "FROM outbox b JOIN orders o ON o.order_id=b.order_id JOIN checkout_sessions s ON s.order_id=o.order_id "
            f"WHERE b.id={new} AND b.event_type='OrderConfirmed'",
        )
        require(len(source) == 1, "new confirmed order missing")
        order_id = source[0]["order_id"]
        require(bool(re.fullmatch(r"cr_[a-zA-Z0-9_-]+", order_id)), "invalid order identity")
        confirmed_count = rows(
            args.source_config,
            f"SELECT count(*) count FROM outbox WHERE order_id='{order_id}' AND event_type='OrderConfirmed'",
        )
        require(len(confirmed_count) == 1 and confirmed_count[0]["count"] == 1, "duplicate confirmed events")
        items = rows(
            args.source_config,
            "SELECT product_key,quantity,unit_amount_minor,stripe_price_id "
            f"FROM order_items WHERE order_id='{order_id}'",
        )
        activation = query(
            "SELECT a.outbox_watermark,f.fence_mode FROM review_automation_activation a "
            "JOIN review_automation_fence f USING(store_key,environment) "
            "WHERE a.store_key='acorn-store' AND a.environment='test'"
        )
        checkpoint = rows(
            args.source_config,
            "SELECT subscriber_id,installation_id,source_database_id,stripe_account_id,catalogue_version,"
            "mapping_sha256,activation_watermark,last_outbox_id FROM reviews_subscriber_checkpoint",
        )
        old_intake = query(f"SELECT disposition FROM review_automation_intake WHERE {where} AND outbox_id={old}")
        old_intent = query(f"SELECT outbox_id FROM review_automation_intents WHERE {where} AND outbox_id={old}")
        intake = query(
            "SELECT outbox_id,acquisition_id,disposition FROM review_automation_intake "
            f"WHERE {where} AND outbox_id={new}"
        )
        intent = query(
            f"SELECT outbox_id,acquisition_id,state FROM review_automation_intents WHERE {where} AND outbox_id={new}"
        )
    require(stat.S_IMODE(args.stripe_key_file.stat().st_mode) == 0o600, "Stripe key file must have mode 600")
    key = args.stripe_key_file.read_text().strip()
    require(key.startswith(("sk_test_", "rk_test_")), "Stripe key must be test-only")
    require(stripe_get(key, "account").get("id") == STRIPE_ACCOUNT, "Stripe account mismatch")
    session_id = source[0]["external_session_id"]
    require(bool(re.fullmatch(r"cs_test_[A-Za-z0-9]+", session_id)), "invalid test session")
    session = stripe_get(key, "checkout/sessions/" + session_id)
    lines = stripe_get(key, "checkout/sessions/" + session_id + "/line_items?limit=10")
    require(
        deployed_version(args.source_config, source_bindings) == active_version, "source changed during qualification"
    )
    require(
        deployed_version(args.reviews_config, reviews_bindings) == reviews_version,
        "reviews changed during qualification",
    )
    require(
        deployed_version(args.producer_config, producer_bindings) == producer_version,
        "producer changed during qualification",
    )
    receipt = validate(
        old,
        old_source,
        source,
        items,
        activation,
        checkpoint,
        old_intake,
        old_intent,
        intake,
        intent,
        session,
        lines,
        expected_checkpoint,
    )
    receipt.update(
        source_worker=configs[0]["name"],
        source_version=active_version,
        source_database_id=source_id,
        reviews_worker=configs[1]["name"],
        reviews_version=reviews_version,
        reviews_database_id=reviews_id,
        producer_version=producer_version,
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
