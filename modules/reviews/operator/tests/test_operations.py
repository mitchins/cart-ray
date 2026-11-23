"""Operator state safety, resumability, and suppression identity checks."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
spec = importlib.util.spec_from_file_location("tested_operator", MODULE / "operate.py")
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)


_clouds = []


@pytest.fixture(autouse=True)
def close_test_databases():
    yield
    while _clouds:
        cloud = _clouds.pop()
        for _, connection in cloud.databases.values():
            connection.close()


def planned(tmp_path):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    return operations.Installation.plan(
        tmp_path / "installation",
        {
            "namespace": "acorn-operator-test",
            "account_id": "a" * 32,
            "stripe_account_id": "acct_Test",
            "preflight_lock": str(lock),
        },
    )


def test_plan_is_nonsecret_offline_and_test_only(tmp_path):
    installation = planned(tmp_path)
    assert installation.state["resources"] == {}
    assert not installation.vault_path.exists()
    for namespace in ["production", "prod", "not-test/../other", "Acorn-test"]:
        with pytest.raises(ValueError, match="namespace"):
            operations.validate_config(installation.config | {"namespace": namespace})
    with pytest.raises(ValueError, match="checkbox default requires"):
        operations.validate_config(installation.config | {"review_invitation_default_checked": False})


def test_failed_merchant_snapshot_can_be_retried(tmp_path, monkeypatch):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    source = tmp_path / "merchant"
    source.mkdir()
    target = tmp_path / "installation"
    config = {"namespace": "acorn-operator-test", "account_id": "a" * 32,
              "stripe_account_id": "acct_Test", "preflight_lock": str(lock), "merchant_inputs": str(source),
              "merchant_name": "Test merchant", "store_key": "test-store", "subject_map": {}, "subject_details": []}
    monkeypatch.setattr(operations, "validate_merchant_inputs", lambda *_: "digest")

    def fail_snapshot(_source, destination, _root):
        destination.mkdir(parents=True)
        raise OSError("copy interrupted")

    monkeypatch.setattr(operations, "snapshot_merchant_inputs", fail_snapshot)
    with pytest.raises(OSError, match="copy interrupted"):
        operations.Installation.plan(target, config)
    assert not (target / "inputs").exists()
    monkeypatch.setattr(operations, "snapshot_merchant_inputs", lambda *_: "a" * 64)
    assert operations.Installation.plan(target, config).state["phase"] == "planned"


def test_theme_selection_and_gel_inputs_survive_original_handoff(tmp_path):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    source = tmp_path / "brand"
    (source / "assets").mkdir(parents=True)
    (source / "assets/mark.svg").write_text("<svg/>")
    (source / "assets/product.svg").write_text("<svg/>")
    (source / "assets/detail.svg").write_text("<svg/>")
    (source / "credentials.json").write_text("private")
    (source / "assets/unreferenced.svg").write_text("private")
    manifest = source / "gel.json"
    manifest.write_text(json.dumps({"assets": {"logo": "assets/mark.svg"}}))
    content = tmp_path / "content.json"
    content.write_text(
        json.dumps(
            {
                "title": "Sample",
                "products": [
                    {
                        "image": "assets/product.svg",
                        "images": [
                            {"url": "assets/product.svg", "alt": "Primary"},
                            {"url": "assets/detail.svg", "alt": "Detail"},
                        ],
                    }
                ],
            }
        )
    )
    installation = operations.Installation.plan(
        tmp_path / "installation",
        {
            "namespace": "acorn-operator-test",
            "account_id": "a" * 32,
            "stripe_account_id": "acct_Test",
            "preflight_lock": str(lock),
            "theme": "classic",
            "gel": str(manifest),
            "gel_assets": str(source),
            "theme_content": str(content),
        },
    )
    copied = installation.directory / "inputs/gel-assets"
    assert sorted(str(path.relative_to(copied)) for path in copied.rglob("*") if path.is_file()) == [
        "assets/detail.svg",
        "assets/mark.svg",
        "assets/product.svg",
    ]
    manifest.unlink()
    content.unlink()
    (source / "assets/mark.svg").unlink()
    resumed = operations.Installation(installation.directory)
    assert resumed.config["theme"] == "classic"
    assert json.loads(Path(resumed.config["gel"]).read_text())["assets"]["logo"] == "assets/mark.svg"
    assert (Path(resumed.config["gel_assets"]) / "assets/mark.svg").read_text() == "<svg/>"
    assert json.loads(Path(resumed.config["theme_content"]).read_text())["title"] == "Sample"


def test_invalid_theme_is_rejected(tmp_path):
    installation = planned(tmp_path)
    with pytest.raises(ValueError, match="theme must be"):
        operations.validate_config(installation.config | {"theme": "unknown"})


def test_plan_rejects_overlapping_asset_root_before_writing_inputs(tmp_path):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    manifest = tmp_path / "gel.json"
    manifest.write_text("{}")
    directory = tmp_path / "installation"
    with pytest.raises(ValueError, match="must not overlap"):
        operations.Installation.plan(
            directory,
            {
                "namespace": "acorn-operator-test",
                "account_id": "a" * 32,
                "stripe_account_id": "acct_Test",
                "preflight_lock": str(lock),
                "gel": str(manifest),
                "gel_assets": str(tmp_path),
            },
        )
    assert not directory.exists()


def test_generated_keys_are_stable_and_loss_never_regenerates(tmp_path):
    installation = planned(tmp_path)
    value = installation.vault(create=True)
    assert installation.vault(create=True) == value
    inventory = installation.state_path.read_text()
    assert value["recipient_hash"] not in inventory
    installation.vault_path.unlink()
    with pytest.raises(ValueError, match="restore"):
        installation.vault(create=True)


def test_changed_suppression_key_is_detected_before_provider_write(tmp_path):
    installation = planned(tmp_path)
    value = installation.vault(create=True)
    value["recipient_hash"] = "changed"
    operations.atomic_json(installation.vault_path, value)
    with pytest.raises(ValueError, match="identity changed"):
        installation.vault()


def test_existing_unmanaged_resource_is_not_adopted(tmp_path):
    installation = planned(tmp_path)
    installation.credentials = {"stripe_test_key": "sk_test_only"}

    class Provider:
        def request(self, path):
            return {"subdomain": "test-account"}

        def listing(self, path):
            return [{"name": installation.name("commerce"), "uuid": "existing"}]

    installation.api = Provider()
    with patch.object(operations, "stripe", return_value={"id": "acct_Test"}):
        with pytest.raises(ValueError, match="unmanaged"):
            installation.provision()


class SQLiteCloud:
    """Actual schema execution with an in-memory provider resource catalog."""

    def __init__(self):
        import sqlite3

        self.sqlite3 = sqlite3
        self.databases = {}
        self.writes = 0
        _clouds.append(self)

    def add_database(self, identifier, name):
        connection = self.sqlite3.connect(":memory:")
        connection.row_factory = self.sqlite3.Row
        self.databases[identifier] = (name, connection)

    def request(self, path):
        if path.startswith("d1/database/"):
            name, _ = self.databases[path.rsplit("/", 1)[1]]
            return {"name": name}
        raise AssertionError("unexpected provider mutation or lookup")

    def query(self, database, sql, params=()):
        _, connection = self.databases[database]
        if not sql.startswith("SELECT"):
            self.writes += 1
        cursor = connection.execute(sql, params)
        connection.commit()
        return [dict(row) for row in cursor.fetchall()]

    def execute(self, database, sql):
        self.writes += 1
        self.databases[database][1].executescript(sql)


def migrated(tmp_path):
    installation = planned(tmp_path)
    installation.vault(create=True)
    cloud = SQLiteCloud()
    for role in operations.DATABASES:
        identifier = role + "-id"
        installation.state["resources"]["database:" + role] = {"id": identifier, "name": installation.name(role)}
        cloud.add_database(identifier, installation.name(role))
    installation.api = cloud
    installation.execute_file = cloud.execute
    installation.save()
    return installation, cloud


def test_shared_real_sqlite_migration_chains_resume_without_reapplication(tmp_path):
    installation, cloud = migrated(tmp_path)
    first = installation.migrate()
    assert first == {"commerce": 11, "subscriber": 2, "reviews": 34, "context": 2}
    counts = {
        role: len(cloud.query(installation.db(role), "SELECT * FROM cartray_operator_migrations"))
        for role in operations.DATABASES
    }
    key = installation.vault()["recipient_hash"]
    assert installation.migrate() == first
    assert installation.vault()["recipient_hash"] == key
    assert counts == {
        role: len(cloud.query(installation.db(role), "SELECT * FROM cartray_operator_migrations"))
        for role in operations.DATABASES
    }
    writes = cloud.writes
    installation.pin_suppression()
    installation.check_schema()
    assert cloud.writes == writes


def test_changed_or_unknown_migration_is_rejected(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.migrate()
    cloud.query(
        installation.db("reviews"), "UPDATE cartray_operator_migrations SET checksum='changed' WHERE position=1"
    )
    with pytest.raises(ValueError, match="changed migration"):
        installation.migrate()
    with pytest.raises(ValueError, match="live migration ledger"):
        installation.check_schema()


def test_foreign_database_identity_is_rejected_before_schema_write(tmp_path):
    installation, cloud = migrated(tmp_path)
    name, connection = cloud.databases[installation.db("commerce")]
    cloud.databases[installation.db("commerce")] = ("different-application-db", connection)
    with pytest.raises(ValueError, match="remote database identity"):
        installation.migrate()
    assert cloud.writes == 0


def test_suppression_current_mismatch_is_never_overwritten(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.migrate()
    # Simulate a different retained server identity, without modifying installed triggers.
    original = cloud.query

    def query(database, sql, params=()):
        if "SELECT c.key_version,r.key_check" in sql:
            return [{"key_version": "different", "key_check": "0" * 64}]
        return original(database, sql, params)

    cloud.query = query
    writes = cloud.writes
    with pytest.raises(ValueError, match="suppression key identity mismatch"):
        installation.pin_suppression()
    assert cloud.writes == writes


def test_rollback_rejects_schema_drift_and_absent_release_without_mutation(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.migrate()
    writes = cloud.writes
    with pytest.raises(ValueError, match="not in this installation"):
        installation.rollback(-2)
    assert cloud.writes == writes


def test_shared_bindings_and_namespace_tampering_fail_closed(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.migrate()
    assert len(cloud.databases) == 1
    assert {installation.db(role) for role in ("commerce", "reviews", "context")} == {"commerce-id"}
    assert len(cloud.query("commerce-id", "SELECT * FROM cartray_operator_migrations")) == 49
    cloud.query("commerce-id", "INSERT INTO cartray_operator_migrations VALUES ('unknown',1,'unknown','bad')")
    with pytest.raises(ValueError, match="namespace"):
        installation.migrate()
    with pytest.raises(ValueError, match="namespace"):
        installation.check_schema()


def test_commerce_only_migration_and_graph_need_no_review_resources(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.config["reviews_enabled"] = False
    installation.migrate()
    assert installation.migrations().keys() == {"commerce"}
    names = {row["name"] for row in cloud.query("commerce-id", "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "review_subjects" not in names
    assert "reviews_subscriber_checkpoint" not in names
    assert "purchaser_context" not in names
    installation.state["workers_subdomain"] = "test-account"
    installation.state["pages_subdomain"] = "test-store.pages.dev"
    nodes, _ = operations.graph(installation)
    assert set(nodes) == {"source"}
    assert not any(binding["type"] == "service" for binding in nodes["source"]["bindings"])
    assert "CARTRAY_REVIEWS_SOURCE" not in nodes["source"]["vars"]
    installation.check_schema()


def test_failed_migration_writes_private_actionable_log(tmp_path):
    from types import SimpleNamespace

    installation = planned(tmp_path)
    installation.credentials = {"cloudflare_api_token": "private-token"}
    log = installation.directory / "migration-error.log"
    log.write_text("old log")
    log.chmod(0o644)
    with patch.object(
        operations.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=1, stdout=b"provider diagnostic\n", stderr=b"failure\n"),
    ):
        with pytest.raises(operations.CloudError, match="protected.*migration-error.log.*retry migrate"):
            installation.execute_file("merchant-db", "SELECT 1;")
    assert log.stat().st_mode & 0o777 == 0o600
    assert "provider diagnostic" in log.read_text()


def test_missing_pinned_registry_is_not_recreated_by_migrate(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.migrate()
    original = cloud.query

    def query(database, sql, params=()):
        if "SELECT c.key_version,r.key_check" in sql:
            return []
        return original(database, sql, params)

    cloud.query = query
    keys = installation.vault()
    with pytest.raises(ValueError, match="restore original registry"):
        installation.migrate()
    assert installation.vault() == keys


@pytest.mark.parametrize("kind", ["file-symlink", "directory-symlink", "missing", "traversal"])
def test_plan_rejects_unsafe_referenced_gel_assets_before_writing(tmp_path, kind):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    source = tmp_path / "brand"
    (source / "assets").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.svg").write_text("private")
    reference = "assets/missing.svg"
    if kind == "file-symlink":
        (source / "assets/linked.svg").symlink_to(outside / "secret.svg")
        reference = "assets/linked.svg"
    elif kind == "directory-symlink":
        (source / "assets/linked").symlink_to(outside, target_is_directory=True)
        reference = "assets/linked/secret.svg"
    elif kind == "traversal":
        reference = "assets/../../outside/secret.svg"
    manifest = source / "gel.json"
    manifest.write_text(json.dumps({"assets": {"logo": reference}}))
    directory = tmp_path / "installation"
    with pytest.raises(ValueError, match="GEL image reference"):
        operations.Installation.plan(
            directory,
            {
                "namespace": "acorn-operator-test",
                "account_id": "a" * 32,
                "stripe_account_id": "acct_Test",
                "preflight_lock": str(lock),
                "gel": str(manifest),
                "gel_assets": str(source),
            },
        )
    assert not directory.exists()


@pytest.mark.parametrize(
    "images",
    [
        [],
        [{"url": "assets/product.svg", "alt": "Primary"}] * 9,
        [{"url": "assets/product.svg", "alt": "Primary"}] * 2,
        [{"url": "assets/../private.svg", "alt": "Unsafe"}],
        [{"url": "assets/product.svg", "alt": ""}],
        [{"url": "https://example.com/product.svg", "alt": "Remote"}],
        [{"url": "assets/other.svg", "alt": "Wrong primary"}],
    ],
)
def test_gel_gallery_snapshot_rejects_invalid_images(tmp_path, images):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/product.svg").write_text("<svg/>")
    manifest = tmp_path / "gel.json"
    manifest.write_text(json.dumps({"assets": {}}))
    content = tmp_path / "content.json"
    content.write_text(json.dumps({"products": [{"image": "assets/product.svg", "images": images}]}))
    with pytest.raises(ValueError):
        operations.Installation.gel_inputs(manifest, tmp_path, content)


def test_fulfilment_migration_binds_existing_installation_and_keeps_key(tmp_path):
    installation, cloud = migrated(tmp_path)
    installation.config["fulfilment"] = {"buckets": {"FILES": "private-test-files"}}
    installation.config["store_key"] = "second-merchant"
    installation.migrate()
    assert cloud.query("commerce-id", "SELECT merchant_id,environment FROM post_purchase_installation") == [
        {"merchant_id": "second-merchant", "environment": "test"}
    ]
    key = installation.vault()["order_access"]
    installation.migrate()
    assert installation.vault()["order_access"] == key
    installation.config["store_key"] = "another-merchant"
    with pytest.raises(ValueError, match="installation scope differs"):
        installation.migrate()


def test_fulfilment_uses_existing_source_with_private_bucket_and_secret_mapping(tmp_path):
    installation, _ = migrated(tmp_path)
    installation.config.update(reviews_enabled=False, store_key="second-merchant",
                               fulfilment={"buckets": {"FILES": "merchant-test-files"}})
    installation.state.update(workers_subdomain="example", pages_subdomain="example.pages.dev")
    installation.credentials = {"stripe_test_key": "sk_test_fixture", "fulfilment_resources": "{}"}
    installation.vault = lambda: {"order_access": "a" * 64, "webhook_secret": "webhook",
                                  "signing_private": "private", "signing_public": "public"}
    nodes, _ = operations.graph(installation)
    source = nodes["source"]
    assert source["vars"]["CARTRAY_ORDER_ACCESS_MODE"] == "test"
    assert source["vars"]["CARTRAY_MERCHANT_ID"] == "second-merchant"
    assert {"name": "FILES", "type": "r2_bucket", "bucket_name": "merchant-test-files"} in source["bindings"]
    secrets = operations.secret_bindings(installation, "source")
    assert {item["name"] for item in secrets} >= {"CARTRAY_ORDER_ACCESS_KEY", "CARTRAY_RESOURCE_CONFIG"}
    assert "CARTRAY_RESOURCE_CONFIG" not in source["vars"]


def test_enabling_fulfilment_adds_key_to_existing_vault_only_before_capabilities_exist(tmp_path):
    installation, cloud = migrated(tmp_path)
    vault = installation.vault()
    vault.pop("order_access")
    operations.atomic_json(installation.vault_path, vault)
    installation.state["secret_fingerprints"].pop("order_access")
    installation.save()
    installation.config["fulfilment"] = {"buckets": {}}
    installation.config["store_key"] = "second-merchant"
    installation.migrate()
    key = installation.vault()["order_access"]
    assert len(key) >= 32
    assert installation.migrate() and installation.vault()["order_access"] == key
    assert cloud.query("commerce-id", "SELECT merchant_id FROM post_purchase_installation") == [
        {"merchant_id": "second-merchant"}
    ]


def test_retained_queue_inventory_is_not_silently_upgraded(tmp_path):
    install = planned(tmp_path)
    install.state["resources"]["queue:events"] = {"id": "retained"}
    install.state["known_uncertain_claim"] = "preserved"
    install.save()
    original = install.state_path.read_bytes()
    with pytest.raises(ValueError, match="pinned release"):
        operations.Installation(install.directory)
    assert install.state_path.read_bytes() == original
    assert not install.vault_path.exists()


def test_new_read_only_reviews_install_has_no_queue_lifecycle(tmp_path):
    install = planned(tmp_path)
    assert install.roles == ("source", "reviews")
    assert install.queues == ()
    assert len(install.secret_names) == 8


def test_receipt_reports_active_order_messages_not_retired_subscriber(tmp_path, monkeypatch):
    install, cloud = migrated(tmp_path)
    install.migrate()
    merchant = install.config.get('store_key', 'acorn-store')
    db = install.db('commerce')
    # Only the real active ledgers are populated; no legacy subscriber exists.
    cloud.query(db, "INSERT INTO post_purchase_installation VALUES (1,?,'test')", [merchant])
    cloud.query(db, "INSERT INTO post_purchase_checkpoint VALUES (1,?,'test',3,4)", [merchant])
    with cloud.databases[db][1] as connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,'order-receipt',?,'accepted',?,1,1)",
                           (merchant,'test','test-order','a'*64,'receipt-provider'))
        connection.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,'old-workflow',?,'accepted',?,1,1)",
                           (merchant,'test','historical-order','f'*64,'historical-provider'))
        connection.execute("INSERT INTO order_review_messages "
                           "(merchant_id,environment,order_id,recipient_hash,invitation_token,unsubscribe_token,"
                           "settings_json,due_at,state,attempted_at,provider_reference,created_at) "
                           "VALUES (?,'test','test-order',?,?,?,'{}',2,'accepted',3,'followup-provider',1)",
                           (merchant,'b'*64,'c'*64,'d'*64))
    monkeypatch.setattr(install, 'doctor', lambda: {'healthy':True})
    result = install.receipt()
    assert result['checkpoint'] == [{'activation_watermark':3,'last_outbox_id':4}]
    assert [(m['purpose'],m['state'],m['provider_reference']) for m in result['order_messages']] == [
        ('order_confirmation','accepted','receipt-provider'),('review_followup','accepted','followup-provider')]
    assert len(result['provider_acceptances']) == 2
    assert not result['legacy_generation_provider_acceptances']
    serialized = json.dumps(result)
    assert 'test-order' not in serialized and 'c'*64 not in serialized and 'd'*64 not in serialized
    assert 'historical-provider' not in serialized
