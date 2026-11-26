"""The opt-in remote runner is prepared safely without using cloud credentials."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

MODULE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("snapshot_remote_runner", MODULE / "tests/installed_snapshot_remote.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

ACCOUNT = "a" * 32
DATABASE = "11111111-2222-3333-4444-555555555555"
NAME = "cartray-snapshot-proof-synthetic-test"


class SQLiteAPI(runner.API):
    """Exercise the exact API.batch wire shape; this does not model hosted atomicity."""

    def __init__(self):
        super().__init__(ACCOUNT, "private-not-printed")
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.writes = 0

    def request(self, path, method="GET", data=None, **_kwargs):
        assert path.startswith("d1/database/" + DATABASE)
        if method == "GET":
            return {"uuid": DATABASE, "name": NAME}
        if "batch" not in data:
            return [{"success": True, "results": [dict(row) for row in self.db.execute(data["sql"])]}]
        self.writes += 1
        self.db.execute("BEGIN")
        try:
            result = [{"success": True, "results": [dict(row) for row in self.db.execute(item["sql"])]}
                      for item in data["batch"]]
            self.db.commit()
            return result
        except sqlite3.IntegrityError as error:
            self.db.rollback()
            raise runner.CloudError("D1 test SQL rejection", provider_errors=[{"message": str(error)}]) from None


@pytest.fixture
def api():
    value = SQLiteAPI()
    yield value
    value.db.close()


def test_synthetic_full_protocol_without_remote_calls(api, tmp_path):
    path = tmp_path / "receipt.json"
    receipt = runner.qualify(api, DATABASE, NAME, path)
    assert receipt["status"] == "PASS"
    assert all(receipt["results"].values())
    assert receipt["fixture_id"] == "approved-synthetic.json"
    assert len(receipt["fixture_sha256"]) == 64
    assert receipt["payloads"]["original"]["sha256"] == receipt["payloads"]["replay"]["sha256"]
    assert "private-not-printed" not in path.read_text()
    assert "body" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="never overwritten"):
        runner.qualify(api, DATABASE, NAME, path)


def test_nonempty_target_is_rejected_without_writing(api, tmp_path):
    api.db.execute("CREATE TABLE existing_customer_data (id TEXT)")
    with pytest.raises(ValueError, match="not empty"):
        runner.qualify(api, DATABASE, NAME, tmp_path / "receipt.json")
    assert api.writes == 0


def test_actual_target_identity_is_checked_before_schema_access(api):
    api.request = Mock(return_value={"uuid": "wrong", "name": NAME})
    with pytest.raises(ValueError, match="identity"):
        runner.assert_empty_target(api, DATABASE, NAME)
    api.request.assert_called_once()


@pytest.mark.parametrize("account,database,name,ack", [
    (ACCOUNT, DATABASE, NAME, False), ("wrong", DATABASE, NAME, True),
    (ACCOUNT, "wrong", NAME, True), (ACCOUNT, DATABASE, "merchant-production", True),
])
def test_target_arguments_require_disposable_scope(account, database, name, ack):
    with pytest.raises(ValueError, match="explicit disposable"):
        runner.target_arguments(account, database, name, ack)


def test_private_credential_file_is_required(tmp_path):
    file = tmp_path / "credentials.json"
    file.write_text(json.dumps({"cloudflare_api_token": "test-token"}))
    file.chmod(0o600)
    assert runner.credentials(file) == "test-token"
    file.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        runner.credentials(file)
    file.chmod(0o600)
    file.write_text(json.dumps({"cloudflare_api_token": "test-token", "stripe_test_key": "unneeded"}))
    with pytest.raises(ValueError, match="only cloudflare_api_token"):
        runner.credentials(file)


@pytest.mark.parametrize("error", [
    runner.CloudError("network unavailable"),
    runner.CloudError("provider error", provider_errors=[{"code": 7500, "message": "internal platform error"}]),
    runner.CloudError("provider error", provider_errors=[{"message": "SQLITE_CONSTRAINT"}]),
])
def test_generic_failure_cannot_count_as_atomicity_proof(error):
    api = Mock()
    api.batch.side_effect = error
    api.query.return_value = []
    with pytest.raises(ValueError, match="inconclusive rejection"):
        runner.rejected(api, DATABASE, {"sql": ["synthetic"], "expected": {}}, {}, "negative")


def test_failure_receipt_retains_hold_without_leaking_provider_message(api, tmp_path):
    original = api.batch

    def fail(sql_database, sql):
        if any("review_publications VALUES" in item for item in sql):
            raise runner.CloudError("unavailable", provider_errors=[{"message": "private provider diagnostic"}])
        return original(sql_database, sql)

    api.batch = fail
    receipt = tmp_path / "held.json"
    with pytest.raises(runner.CloudError):
        runner.qualify(api, DATABASE, NAME, receipt)
    value = json.loads(receipt.read_text())
    assert value["status"] == "HOLD"
    assert value["failure"] == "unavailable"
    assert "private provider diagnostic" not in receipt.read_text()


def test_cli_requires_explicit_acknowledgement(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["installed_snapshot_remote.py"])
    with pytest.raises(SystemExit):
        runner.main([])
