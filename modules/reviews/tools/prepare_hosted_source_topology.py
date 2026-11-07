"""Generate non-secret, isolated test configs for the attested source topology.

This does not deploy, mutate resources, read credentials, or attest that the
bindings exist. Review resource identities before using the generated files.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

ID = re.compile(r"[A-Za-z0-9_-]{1,63}\Z")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
HEX32 = re.compile(r"[a-f0-9]{32}\Z")
CAT = re.compile(r"sha256:[a-f0-9]{64}\Z")
ROOT = Path(__file__).resolve().parents[3]


def quoted(value):
    return json.dumps(value, ensure_ascii=True)


def build(config):
    """Fail closed on invalid or extra fields before generating any files."""
    expected = {
        "account_id",
        "stripe_account_id",
        "commerce_db_id",
        "commerce_db_name",
        "reviews_db_id",
        "reviews_db_name",
        "source_worker",
        "producer_worker",
        "consumer_worker",
        "reviews_worker",
        "events_queue_name",
        "events_queue_id",
        "dlq_name",
        "dlq_id",
        "store",
        "issuer",
        "installation",
        "catalogue_version",
        "subject_mapping",
        "storefront_origin",
        "signing_key_id",
        "policy_version",
        "delay_seconds",
        "max_lateness",
    }
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError("invalid topology fields")
    names = (
        "commerce_db_name",
        "reviews_db_name",
        "source_worker",
        "producer_worker",
        "consumer_worker",
        "reviews_worker",
        "events_queue_name",
        "dlq_name",
        "store",
        "issuer",
        "installation",
        "signing_key_id",
        "policy_version",
    )
    if any(not isinstance(config[k], str) or not ID.fullmatch(config[k]) for k in names):
        raise ValueError("invalid topology identifier")
    if not HEX32.fullmatch(config["account_id"]) or any(
        not UUID.fullmatch(config[k]) for k in ("commerce_db_id", "reviews_db_id")
    ):
        raise ValueError("invalid account or database identifier")
    if any(not HEX32.fullmatch(config[k]) for k in ("events_queue_id", "dlq_id")):
        raise ValueError("invalid queue identifier")
    if config["commerce_db_id"] == config["reviews_db_id"] or config["events_queue_id"] == config["dlq_id"]:
        raise ValueError("resources must be distinct")
    if not re.fullmatch(r"acct_[A-Za-z0-9]+", config["stripe_account_id"]):
        raise ValueError("invalid Stripe account")
    if not CAT.fullmatch(config["catalogue_version"]) or not re.fullmatch(
        r"https://[a-z0-9.-]+", config["storefront_origin"]
    ):
        raise ValueError("invalid catalogue or storefront origin")
    if any(type(config[k]) is not int or not 0 <= config[k] <= 366 * 86400 for k in ("delay_seconds", "max_lateness")):
        raise ValueError("invalid scheduling policy")
    mapping = config["subject_mapping"]
    if (
        not isinstance(mapping, dict)
        or not mapping
        or any(
            not isinstance(k, str) or not ID.fullmatch(k) or not isinstance(v, str) or not ID.fullmatch(v)
            for k, v in mapping.items()
        )
    ):
        raise ValueError("invalid subject mapping")
    mapping_text = json.dumps({config["catalogue_version"]: mapping}, sort_keys=True, separators=(",", ":"))
    mapping_sha256 = hashlib.sha256(mapping_text.encode()).hexdigest()
    q = quoted
    c = config
    common = f'account_id = {q(c["account_id"])}\ncompatibility_date = "2026-08-31"\n'
    source = f"""name = {q(c["source_worker"])}
{common}main = {q(str(ROOT / "src/cartray/worker.py"))}
compatibility_flags = ["python_workers"]
workers_dev = true
[build]
command = "uv run python scripts/sync_worker_source.py"
cwd = {q(str(ROOT))}
[triggers]
crons = ["0 * * * *"]
[version_metadata]
binding = "CF_VERSION_METADATA"
[vars]
CARTRAY_ENVIRONMENT = "test"
CARTRAY_REVIEWS_SOURCE = "test"
CARTRAY_REVIEWS_PRODUCER = "test"
CARTRAY_SIGNING_KEY_ID = {q(c["signing_key_id"])}
CARTRAY_STOREFRONT_ORIGIN = {q(c["storefront_origin"])}
CARTRAY_SUCCESS_URL = {q(c["storefront_origin"] + "/?checkout=complete")}
CARTRAY_CANCEL_URL = {q(c["storefront_origin"] + "/?checkout=cancelled")}
[[d1_databases]]
binding = "DB"
database_name = {q(c["commerce_db_name"])}
database_id = {q(c["commerce_db_id"])}
migrations_dir = {q(str(ROOT / "migrations"))}
[[services]]
binding = "REVIEWS_SOURCE_TRIGGER"
service = {q(c["producer_worker"])}
[[ratelimits]]
name = "CHECKOUT_RATE_LIMITER"
namespace_id = "20261107"
simple = {{ limit = 20, period = 60 }}
"""
    reviews = f"""name = {q(c["reviews_worker"])}
{common}main = {q(str(ROOT / "modules/reviews/src/worker.py"))}
compatibility_flags = ["python_workers", "disable_python_external_sdk"]
workers_dev = false
[vars]
REVIEWS_MODE = "verified_acquisition_only"
REVIEWS_AUTOMATION_INTAKE = "test"
REVIEWS_ENVIRONMENT = "test"
REVIEWS_STORE_KEY = {q(c["store"])}
REVIEWS_SUBJECT_MAPS = {q(mapping_text)}
[[ratelimits]]
name = "REVIEWS_INGRESS"
namespace_id = "20261108"
simple = {{ limit = 1000, period = 60 }}
[[d1_databases]]
binding = "REVIEWS_DB"
database_name = {q(c["reviews_db_name"])}
database_id = {q(c["reviews_db_id"])}
migrations_dir = {q(str(ROOT / "modules/reviews/migrations"))}
[[d1_databases]]
binding = "CARTRAY_DB"
database_name = {q(c["commerce_db_name"])}
database_id = {q(c["commerce_db_id"])}
"""
    consumer = f"""name = {q(c["consumer_worker"])}
{common}main = {q(str(ROOT / "modules/reviews/automation-intake-queue/worker.mjs"))}
workers_dev = false
[vars]
REVIEWS_ENVIRONMENT = "test"
REVIEWS_ORDER_SUBSCRIBER = "test"
SOURCE_STORE = {q(c["store"])}
SOURCE_ISSUER = {q(c["issuer"])}
SOURCE_INSTALLATION = {q(c["installation"])}
[[services]]
binding = "REVIEWS_API"
service = {q(c["reviews_worker"])}
[[queues.consumers]]
queue = {q(c["events_queue_name"])}
max_batch_size = 10
max_batch_timeout = 1
max_retries = 3
dead_letter_queue = {q(c["dlq_name"])}
"""
    producer = f"""name = {q(c["producer_worker"])}
{common}main = {q(str(ROOT / "modules/reviews/automation-intake-producer/worker.mjs"))}
workers_dev = false
[triggers]
crons = ["0 * * * *"]
[version_metadata]
binding = "CF_VERSION_METADATA"
[vars]
REVIEWS_ENVIRONMENT = "test"
REVIEWS_SOURCE_PRODUCER = "test"
CF_ACCOUNT_ID = {q(c["account_id"])}
SOURCE_DATABASE_ID = {q(c["commerce_db_id"])}
STRIPE_ACCOUNT_ID = {q(c["stripe_account_id"])}
SOURCE_WORKER = {q(c["source_worker"])}
PRODUCER_WORKER = {q(c["producer_worker"])}
REVIEWS_WORKER = {q(c["reviews_worker"])}
CONSUMER_WORKER = {q(c["consumer_worker"])}
EVENT_QUEUE_NAME = {q(c["events_queue_name"])}
EVENT_QUEUE_ID = {q(c["events_queue_id"])}
DEAD_LETTER_QUEUE_NAME = {q(c["dlq_name"])}
DEAD_LETTER_QUEUE_ID = {q(c["dlq_id"])}
SOURCE_STORE = {q(c["store"])}
SOURCE_ISSUER = {q(c["issuer"])}
SOURCE_INSTALLATION = {q(c["installation"])}
CATALOGUE_VERSION = {q(c["catalogue_version"])}
MAPPING_SHA256 = {q(mapping_sha256)}
POLICY_VERSION = {q(c["policy_version"])}
DELAY_SECONDS = {c["delay_seconds"]}
MAX_LATENESS = {c["max_lateness"]}
[[d1_databases]]
binding = "COMMERCE_DB"
database_name = {q(c["commerce_db_name"])}
database_id = {q(c["commerce_db_id"])}
[[services]]
binding = "SOURCE_API"
service = {q(c["source_worker"])}
[[services]]
binding = "REVIEWS_API"
service = {q(c["reviews_worker"])}
[[queues.producers]]
binding = "EVENT_QUEUE"
queue = {q(c["events_queue_name"])}
"""
    return {"source.toml": source, "reviews.toml": reviews, "consumer.toml": consumer, "producer.toml": producer}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", required=True, type=Path, help="non-secret JSON resource descriptor")
    parser.add_argument("--output", required=True, type=Path, help="new private directory")
    args = parser.parse_args()
    configs = build(json.loads(args.resources.read_text()))
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in configs.items():
        (args.output / name).write_text(content)
    print(f"Prepared {len(configs)} test-only configurations in {args.output}; no resources changed")


if __name__ == "__main__":
    main()
