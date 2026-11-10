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


def test_theme_selection_and_gel_inputs_survive_original_handoff(tmp_path):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    source = tmp_path / "brand"
    (source / "assets").mkdir(parents=True)
    (source / "assets/mark.svg").write_text("<svg/>")
    (source / "assets/product.svg").write_text("<svg/>")
    (source / "credentials.json").write_text("private")
    (source / "assets/unreferenced.svg").write_text("private")
    manifest = source / "gel.json"
    manifest.write_text(json.dumps({"assets": {"logo": "assets/mark.svg"}}))
    content = tmp_path / "content.json"
    content.write_text(json.dumps({"title": "Sample", "products": [{"image": "assets/product.svg"}]}))
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
    assert first == {"commerce": 5, "subscriber": 2, "reviews": 33, "context": 2}
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
    assert len(cloud.query("commerce-id", "SELECT * FROM cartray_operator_migrations")) == 42
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
