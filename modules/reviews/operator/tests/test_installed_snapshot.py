"""Approved artifact application targets the retained installation, never caller SQL."""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_operations import operations

installed_snapshot = __import__("installed_snapshot")

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/reviews"


class Database:
    account = "a" * 32

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        manifest = json.loads((MODULE / "module.json").read_text())
        for migration in manifest["migrations"]:
            self.connection.executescript((MODULE / migration).read_text())
        self.batches = 0

    def batch(self, database, statements):
        assert database == "bound-database"
        self.batches += 1
        self.connection.execute("BEGIN")
        results, statement = [], ""
        try:
            for statement in statements:
                cursor = self.connection.execute(statement)
                results.append([dict(row) for row in cursor.fetchall()])
                statement = ""
            assert not statement.strip()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results


@pytest.fixture
def target(tmp_path):
    api = Database()
    installation = SimpleNamespace(
        config={"reviews_enabled": True, "account_id": api.account, "namespace": "merchant-test",
                "store_key": "example-store"},
        state={"environment": "test", "installation": "merchant-test", "deployment": {"complete": True}},
        directory=tmp_path,
        api=api, db=Mock(return_value="bound-database"), assert_resources=Mock(), verifier=Mock(),
    )
    snapshot = MODULE / "tests/fixtures/approved-synthetic.json"
    approval = {
        "format": 1, "account_id": api.account, "database_id": "bound-database",
        "installation_id": "merchant-test", "store_key": "example-store", "environment": "test",
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(), "snapshot_id": "one",
        "publication_revision": 1, "expected_publication": None, "approval_ref": "review:synthetic-approved",
    }
    yield installation, snapshot, approval
    api.connection.close()


def apply(target, approval=None, **changes):
    installation, snapshot, original = target
    path = installation.directory / "approval.json"
    path.write_text(json.dumps((approval or original) | changes))
    return installed_snapshot.apply(installation, snapshot, path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_exact_target_is_verified_and_receipt_is_immutable(target):
    installation, _, _ = target
    first = apply(target)
    assert first["readback"] == {
        "snapshot_id": "one", "revision": 1, "digest": target[2]["snapshot_sha256"],
        "subjects": 1, "reviews": 3, "replies": 1, "aliases": 0, "titles": 0, "provenance": 1,
    }
    assert apply(target) == first
    installation.assert_resources.assert_called()
    installation.verifier.assert_called_with("inspect")
    assert installation.api.batches == 2
    receipts = list((installation.directory / "review-snapshot-receipts").glob("*.json"))
    assert len(receipts) == 1
    assert receipts[0].stat().st_mode & 0o777 == 0o600
    assert "body" not in receipts[0].read_text()
    assert "Good" not in receipts[0].read_text()


@pytest.mark.parametrize("field,value", [
    ("account_id", "b" * 32), ("database_id", "other"), ("installation_id", "other-test"),
    ("store_key", "other"), ("environment", "live"), ("snapshot_sha256", "0" * 64),
    ("snapshot_id", "different"), ("publication_revision", True), ("format", True),
    ("approval_ref", "arbitrary SQL;"), ("sql", "DROP TABLE review_snapshots"),
    ("expected_publication", {"snapshot_id": "one", "revision": True, "digest": "a" * 64}),
])
def test_approval_mismatch_has_zero_database_writes(target, field, value):
    with pytest.raises(ValueError):
        apply(target, **{field: value})
    assert target[0].api.batches == 0
    target[0].assert_resources.assert_not_called()


def test_modified_approval_bytes_rejected(target):
    install, snapshot, approval = target
    path = install.directory / "approval.json"
    path.write_text(json.dumps(approval))
    with pytest.raises(ValueError, match="SHA-256"):
        installed_snapshot.apply(install, snapshot, path, "0" * 64)
    assert install.api.batches == 0


@pytest.mark.parametrize("change", ["reviews_disabled", "live_inventory", "wrong_api", "wrong_inventory", "drift"])
def test_installed_scope_and_deployment_drift_fail_closed(target, change):
    install = target[0]
    if change == "reviews_disabled":
        install.config["reviews_enabled"] = False
    elif change == "live_inventory":
        install.state["environment"] = "live"
    elif change == "wrong_api":
        install.api.account = "b" * 32
    elif change == "wrong_inventory":
        install.state["installation"] = "different"
    else:
        install.verifier.side_effect = ValueError("binding version mismatch")
    with pytest.raises(ValueError):
        apply(target)
    assert install.api.batches == 0


def test_stale_approved_predecessor_cannot_replace_current_publication(target):
    apply(target)
    db = target[0].api.connection
    db.execute("UPDATE review_publications SET revision=2")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        apply(target)
    assert db.execute("SELECT revision FROM review_publications").fetchone()[0] == 2


def test_late_failure_rolls_back_without_receipt_and_retry_succeeds(target):
    db = target[0].api.connection
    db.executescript("CREATE TRIGGER fail_publication BEFORE INSERT ON review_publications "
                     "BEGIN SELECT RAISE(ABORT, 'late fail'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        apply(target)
    assert db.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 0
    assert not (target[0].directory / "review-snapshot-receipts").exists()
    db.execute("DROP TRIGGER fail_publication")
    apply(target)


def test_unknown_result_writes_no_receipt(target):
    target[0].api.batch = Mock(return_value=[[{"different": True}]])
    with pytest.raises(ValueError, match="readback"):
        apply(target)
    assert not (target[0].directory / "review-snapshot-receipts").exists()


def test_receipt_cannot_be_replaced(target):
    result = apply(target)
    with pytest.raises(ValueError, match="immutable"):
        installed_snapshot.save_receipt(target[0].directory / "review-snapshot-receipts",
                                        result["approval_sha256"], result | {"approval_ref": "other"})


def test_cli_accepts_artifact_and_approval_not_sql(target, monkeypatch, capsys):
    install, snapshot, approval = target
    file = install.directory / "approval.json"
    file.write_text(json.dumps(approval))
    args = ["operate.py", "--directory", str(install.directory), "apply-review-snapshot",
            "--snapshot", str(snapshot), "--approval", str(file),
            "--approval-sha256", hashlib.sha256(file.read_bytes()).hexdigest()]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(operations, "Installation", Mock(return_value=install))
    operations.main()
    assert json.loads(capsys.readouterr().out)["readback"]["reviews"] == 3


def test_ignored_contribution_aborts_count_guard_before_commit(target):
    db = target[0].api.connection
    db.executescript("CREATE TRIGGER skip_contribution BEFORE INSERT ON review_contributions "
                     "WHEN NEW.contribution_id='r' BEGIN SELECT RAISE(IGNORE); END;")
    with pytest.raises(sqlite3.IntegrityError):
        apply(target)
    assert db.execute("SELECT COUNT(*) FROM review_publications").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM review_contributions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 0
    assert not (target[0].directory / "review-snapshot-receipts").exists()


def test_undeployed_inventory_has_useful_diagnostic(target):
    target[0].state.pop("deployment")
    with pytest.raises(ValueError, match="deploy and verify"):
        apply(target)
    assert target[0].api.batches == 0


def test_missing_provenance_is_rejected_inside_transaction(target):
    db = target[0].api.connection
    db.executescript("CREATE TRIGGER skip_provenance BEFORE INSERT ON review_snapshot_provenance "
                     "BEGIN SELECT RAISE(IGNORE); END;")
    with pytest.raises(sqlite3.IntegrityError):
        apply(target)
    assert db.execute("SELECT COUNT(*) FROM review_publications").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 0


def test_duplicate_approval_keys_rejected_even_with_matching_digest(target):
    install, snapshot, approval = target
    path = install.directory / "approval.json"
    path.write_text(json.dumps(approval)[:-1] + ',"environment":"test"}')
    approval_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="duplicate"):
        installed_snapshot.apply(install, snapshot, path, approval_hash)
    assert install.api.batches == 0


def test_oversized_approval_is_rejected_before_provider_work(target):
    install, snapshot, _ = target
    path = install.directory / "approval.json"
    path.write_bytes(b" " * 16_385)
    approval_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="supported import size"):
        installed_snapshot.apply(install, snapshot, path, approval_hash)
    assert install.api.batches == 0
