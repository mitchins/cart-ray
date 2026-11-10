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


def test_all_three_real_sqlite_migration_chains_resume_without_reapplication(tmp_path):
    installation, cloud = migrated(tmp_path)
    first = installation.migrate()
    assert first == {"commerce": 7, "reviews": 33, "context": 2}
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
