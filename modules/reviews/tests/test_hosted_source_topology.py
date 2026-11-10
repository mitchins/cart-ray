"""Verify generated hosted configurations retain explicit test resource identities."""

import importlib.util
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/prepare_hosted_source_topology.py"
spec = importlib.util.spec_from_file_location("hosted_topology", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def config():
    return {
        "account_id": "a" * 32,
        "stripe_account_id": "acct_123",
        "commerce_db_id": "11111111-1111-4111-8111-111111111111",
        "commerce_db_name": "commerce-test",
        "reviews_db_id": "22222222-2222-4222-8222-222222222222",
        "reviews_db_name": "reviews-test",
        "source_worker": "source-test",
        "producer_worker": "producer-test",
        "consumer_worker": "consumer-test",
        "reviews_worker": "reviews-test",
        "events_queue_name": "events-test",
        "events_queue_id": "b" * 32,
        "dlq_name": "dlq-test",
        "dlq_id": "c" * 32,
        "store": "acorn-store",
        "issuer": "issuer-v1",
        "installation": "installation-1",
        "catalogue_version": "sha256:" + "d" * 64,
        "subject_mapping": {"ACORN-KIT": "acorn-kit"},
        "storefront_origin": "https://acorn-test.pages.dev",
        "signing_key_id": "key-v1",
        "policy_version": "policy-v1",
        "delay_seconds": 10,
        "max_lateness": 100,
    }


def test_generated_configs_bind_distinct_resources_and_never_embed_secrets():
    result = {name: tomllib.loads(text) for name, text in module.build(config()).items()}
    assert set(result) == {"source.toml", "reviews.toml", "consumer.toml", "producer.toml"}
    source, reviews, consumer, producer = (result[x] for x in result)
    assert source["d1_databases"][0]["database_id"] == config()["commerce_db_id"]
    assert reviews["d1_databases"][0]["database_id"] == config()["reviews_db_id"]
    assert reviews["d1_databases"][1]["database_id"] == config()["commerce_db_id"]
    assert producer["d1_databases"][0]["database_id"] == config()["commerce_db_id"]
    assert producer["vars"]["STRIPE_ACCOUNT_ID"] == "acct_123"
    assert all("secret" not in text.lower() and "sk_test_" not in text for text in module.build(config()).values())
    assert all(item.get("workers_dev") is False for item in (reviews, consumer, producer))
    assert source["vars"]["CARTRAY_ENVIRONMENT"] == "test"


@pytest.mark.parametrize(
    "changes",
    [
        {"events_queue_id": "c" * 32},
        {"events_queue_name": "dlq-test"},
        {"producer_worker": "source-test"},
        {"consumer_worker": "reviews-test"},
        {"source_worker": "bad/name"},
        {"stripe_account_id": "other"},
        {"storefront_origin": "http://example.test"},
        {"delay_seconds": True},
        {"subject_mapping": {}},
        {"unexpected": "field"},
    ],
)
def test_invalid_topology_is_rejected(changes):
    with pytest.raises(ValueError):
        module.build(config() | changes)


def test_subscriber_checkpoint_identity_is_immutable():
    import sqlite3

    db = sqlite3.connect(":memory:")
    migration = module.ROOT / "modules/reviews/automation-intake-producer/migrations/0001_reviews_subscriber.sql"
    db.executescript(migration.read_text())
    row = ("issuer", "install", "db", "acct", "catalogue", "a" * 64, 10, 10)
    db.execute("INSERT INTO reviews_subscriber_checkpoint VALUES (?,?,?,?,?,?,?,?)", row)
    db.execute("UPDATE reviews_subscriber_checkpoint SET last_outbox_id=11 WHERE subscriber_id=?", ("issuer",))
    for query in (
        "DELETE FROM reviews_subscriber_checkpoint WHERE subscriber_id='issuer'",
        "UPDATE reviews_subscriber_checkpoint SET last_outbox_id=9 WHERE subscriber_id='issuer'",
        "UPDATE reviews_subscriber_checkpoint SET installation_id='other' WHERE subscriber_id='issuer'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(query)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT OR REPLACE INTO reviews_subscriber_checkpoint VALUES (?,?,?,?,?,?,?,?)", row)
    assert db.execute("SELECT installation_id,last_outbox_id FROM reviews_subscriber_checkpoint").fetchone() == (
        "install",
        11,
    )


def test_shared_merchant_database_is_valid():
    value = config()
    value["reviews_db_id"] = value["commerce_db_id"]
    module.build(value)
