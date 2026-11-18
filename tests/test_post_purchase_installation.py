"""No-backfill initialisation must be atomic, test-only, and non-repairing."""

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pp_installation", ROOT / "scripts/post_purchase_installation.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE orders(order_id TEXT PRIMARY KEY)")
    for name in ("0006_post_purchase_actions.sql", "0007_post_purchase_checkpoint.sql"):
        db.executescript((ROOT / "migrations" / name).read_text())
    return db


def batch(db, apply=False, queries=None):
    with db:
        for item in queries or installer.statements("merchant", apply):
            rows = db.execute(item["sql"], item["params"]).fetchall()
    return dict(rows[0])


def test_default_dry_run_and_atomic_fence_no_backfill():
    with database() as db:
        db.executemany("INSERT INTO outbox VALUES (?)", [(1,), (7,)])
        status = batch(db)
        assert installer.classify(status, "merchant") == "uninitialised"
        assert status["installation_count"] == status["checkpoint_count"] == 0
        status = batch(db, True)
        assert installer.classify(status, "merchant") == "retained"
        assert status["activation_watermark"] == status["last_outbox_id"] == 7
        db.execute("INSERT INTO outbox VALUES (8)")
        db.execute("UPDATE post_purchase_checkpoint SET last_outbox_id=8")
        again = batch(db, True)
        assert again["activation_watermark"] == 7
        assert again["last_outbox_id"] == 8  # Never resets an advanced cursor.


def test_empty_outbox_initialises_zero_and_partial_insert_rolls_back():
    with database() as db:
        queries = installer.statements("merchant", True)
        queries[2] = {"sql": "INSERT INTO missing_table VALUES (1)", "params": []}
        with pytest.raises(sqlite3.OperationalError):
            batch(db, queries=queries)
        assert db.execute("SELECT COUNT(*) FROM post_purchase_installation").fetchone()[0] == 0
        assert batch(db, True)["activation_watermark"] == 0


@pytest.mark.parametrize("seed", [
    "INSERT INTO post_purchase_installation VALUES (1,'merchant','test')",
    "INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','test',0,0)",
    "INSERT INTO post_purchase_installation VALUES (1,'other','test');"
    "INSERT INTO post_purchase_checkpoint VALUES (1,'other','test',0,0)",
    "INSERT INTO post_purchase_installation VALUES (1,'merchant','live');"
    "INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','live',0,0)",
    "INSERT INTO post_purchase_installation VALUES (1,'merchant','test');"
    "INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','test',5,5)",
])
def test_partial_or_changed_scope_refused_without_repair(seed):
    with database() as db:
        db.executescript(seed)
        before = list(db.iterdump())
        with pytest.raises(ValueError, match="no repair"):
            installer.classify(batch(db), "merchant")
        with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
            batch(db, True)
        assert list(db.iterdump()) == before


def test_scope_guard_rechecks_after_dry_run():
    with database() as db:
        assert installer.classify(batch(db), "merchant") == "uninitialised"
        db.executescript("INSERT INTO post_purchase_installation VALUES (1,'other','test')")
        with pytest.raises(sqlite3.OperationalError):
            batch(db, True)
        assert db.execute("SELECT COUNT(*) FROM post_purchase_checkpoint").fetchone()[0] == 0


@pytest.mark.parametrize("merchant,environment", [("other", "test"), ("merchant", "live"), ("other", "live")])
def test_retained_foreign_executions_are_rejected_by_status_and_atomic_guard(merchant, environment):
    with database() as db:
        batch(db, True)
        db.execute("INSERT INTO orders VALUES ('order-one')")
        db.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,?,?,'claimed',NULL,0,0)",
                   (merchant, environment, "order-one", "workflow-one", "a" * 64))
        db.commit()
        before = list(db.iterdump())
        status = batch(db)
        assert status["foreign_execution_count"] == 1
        with pytest.raises(ValueError, match="no repair"):
            installer.classify(status, "merchant")
        with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
            batch(db, True)
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("state", ["claimed", "accepted", "uncertain"])
def test_retained_valid_execution_claims_are_unchanged(state):
    with database() as db:
        batch(db, True)
        db.execute("INSERT INTO orders VALUES ('order-one')")
        db.execute("INSERT INTO post_purchase_executions VALUES ('merchant','test',?,?,?,?,?,0,0)",
                   ("order-one", "workflow-one", "a" * 64, state, "provider-reference"))
        db.commit()
        before = list(db.iterdump())
        assert installer.classify(batch(db, True), "merchant") == "retained"
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("mode,environment,database_id", [
    ("test", "test", "db"), ("disabled", "live", "db"), ("disabled", "test", "other"),
])
def test_backend_must_be_correct_test_binding_with_processing_disabled(mode, environment, database_id):
    installation = SimpleNamespace(db=lambda role: "db", name=lambda role: "merchant-test-" + role,
                                   state={"deployment": {"versions": {"source": "v1"}}})
    api = SimpleNamespace(request=lambda path: {"name": "merchant-test-commerce"}, version=lambda name: {
        "id": "v1", "resources": {"bindings": [
            {"name": "DB", "type": "d1", "database_id": database_id},
            {"name": "CARTRAY_ENVIRONMENT", "type": "plain_text", "text": environment},
            {"name": "CARTRAY_POST_PURCHASE_MODE", "type": "plain_text", "text": mode},
        ]}})
    with pytest.raises(ValueError, match="processing disabled"):
        installer.validate_backend(installation, api)


@pytest.mark.parametrize("apply", [False, True])
def test_cli_uses_rest_for_status_and_one_import_for_apply(monkeypatch, capsys, apply):
    args = ["post_purchase_installation.py", "--directory", "inventory", "--credentials", "handoff"]
    if apply:
        args += ["--apply", "--confirm-processing-disabled"]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(installer, "Installation", lambda path: SimpleNamespace(
        config={"account_id": "test-account", "store_key": "merchant"}))
    monkeypatch.setattr(installer, "read_private", lambda path: {"cloudflare_api_token": "fixture-token"})
    calls = []
    with database() as db:
        db.execute("INSERT INTO outbox VALUES (7)")

        def query(database_id, sql, params):
            calls.append(("query", database_id, sql, params))
            return [dict(row) for row in db.execute(sql, params)]

        api = SimpleNamespace(query=query)
        monkeypatch.setattr(installer, "API", lambda account, token: api)

        def validate_backend(installation, seen_api):
            assert seen_api is api
            calls.append(("validate",))
            return "fixture-db", "merchant-test-commerce"

        def remote_import(account, db_id, name, token, merchant, diagnostics=None):
            assert apply, "dry run must not start a D1 import"
            calls.append(("import", account, db_id, name, merchant))
            with db:
                for statement in installer.fence_import_sql(merchant).strip().rstrip(";").split(";\n"):
                    db.execute(statement)

        monkeypatch.setattr(installer, "validate_backend", validate_backend)
        monkeypatch.setattr(installer, "remote_import", remote_import)
        installer.main()
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["applied"] is apply and receipt["sendingEnabled"] is False
        assert receipt["state"] == ("retained" if apply else "uninitialised")
        assert receipt["current_outbox_id"] == 7
        writes = [("import", "test-account", "fixture-db", "merchant-test-commerce", "merchant")] if apply else []
        assert calls == [("validate",), *writes, ("query", "fixture-db", installer.STATUS_SQL, ["merchant"])]
        if apply:
            assert receipt["activation_watermark"] == 7
        else:
            assert receipt["activation_watermark"] is None
            assert db.execute("SELECT COUNT(*) FROM post_purchase_installation").fetchone()[0] == 0


# Captured retained-import output, with the bookmark and storage metrics replaced.
IMPORT_PROGRESS = "\u251c Checking if file needs uploading\n\u2502\n"
IMPORT_RECEIPT = json.dumps([{
    "results": [{"Total queries executed": 3, "Rows read": 4, "Rows written": 0, "Database size (MB)": 0.5}],
    "success": True, "finalBookmark": "redacted-bookmark", "meta": {"rows_written": 0, "changed_db": False},
}], indent=2)


@pytest.mark.parametrize("stdout,exit_code,success", [
    ('[{"success": true, "finalBookmark": "fixture", "results": [{"Total queries executed": 3}]}]', 0, True),
    ('[{"success": true, "finalBookmark": "fixture", "results": [{"Total queries executed": 2}]}]', 0, False),
    ('[{"success": true}]', 0, False),
    ('[{"success": false, "finalBookmark": "fixture"}]', 0, False),
    ('[{"success": true, "finalBookmark": "fixture"}]', 1, False),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT + "\n", 0, True, id="captured-retained-import"),
    pytest.param(IMPORT_PROGRESS + "\u251c Uploading file\n\u2502 Uploading complete.\n" + IMPORT_RECEIPT,
                 0, True, id="multiple-progress-lines"),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT + "\ntrailing garbage", 0, False, id="trailing-garbage"),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT + "\n\u2502\n", 0, False, id="trailing-progress"),
    pytest.param(IMPORT_RECEIPT + "\n" + IMPORT_RECEIPT, 0, False, id="two-json-receipts"),
    pytest.param("unexpected prefix\n" + IMPORT_RECEIPT, 0, False, id="unexpected-prefix"),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT[:-1], 0, False, id="malformed-json"),
    pytest.param(IMPORT_PROGRESS, 0, False, id="missing-json"),
    pytest.param(IMPORT_PROGRESS + json.dumps(json.loads(IMPORT_RECEIPT) * 2), 0, False, id="two-results"),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT.replace('"success": true', '"success": false'),
                 0, False, id="failed-result"),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT.replace('"success": true', '"success": false, "success": true'),
                 0, False, id="duplicate-key"),
    pytest.param(IMPORT_PROGRESS + IMPORT_RECEIPT.replace('"changed_db": false', '"changed_db": NaN'),
                 0, False, id="invalid-json-constant"),
])
def test_remote_import_uses_locked_wrangler_sql_file_and_requires_completion(
        monkeypatch, stdout, exit_code, success):
    calls = []

    def run(command, **options):
        assert command[0] == str(ROOT / "node_modules/.bin/wrangler")
        assert command[1:5] == ["d1", "execute", "DB", "--config"]
        assert command[6:8] == ["--remote", "--file"]
        assert command[9:] == ["--yes", "--json"]
        assert json.loads(Path(command[5]).read_text())["d1_databases"] == [
            {"binding": "DB", "database_id": "fixture-id", "database_name": "merchant-test-commerce"}
        ]
        assert json.loads(Path(command[5]).read_text())["account_id"] == "fixture-account"
        calls.append(Path(command[8]).read_text())
        assert options["env"]["CLOUDFLARE_API_TOKEN"] == "fixture-token"
        assert options["env"]["WRANGLER_LOG_SANITIZE"] == "true"
        assert options["env"]["WRANGLER_LOG"] == "log"
        assert "fixture-token" not in command
        assert options["stdin"] == subprocess.DEVNULL and options["capture_output"]
        return SimpleNamespace(returncode=exit_code, stdout=stdout, stderr="fixture-private")

    monkeypatch.setattr(installer.subprocess, "run", run)
    args = ("fixture-account", "fixture-id", "merchant-test-commerce", "fixture-token", "merchant")
    if success:
        installer.remote_import(*args)
    else:
        with pytest.raises(RuntimeError, match="D1 import") as error:
            installer.remote_import(*args)
        assert "fixture-private" not in str(error.value)
    assert calls == [installer.fence_import_sql("merchant")]


def test_sql_file_rejects_literals_outside_merchant_identifier_grammar():
    with pytest.raises(ValueError, match="invalid merchant identity"):
        installer.fence_import_sql("merchant'; DROP TABLE outbox; --")


@pytest.mark.parametrize("timed_out", [False, True])
def test_diagnostics_capture_redacted_import_failure_without_overwriting(monkeypatch, capsys, tmp_path, timed_out):
    diagnostic = tmp_path / "diagnostics.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "post_purchase_installation.py", "--directory", "inventory", "--credentials", "handoff",
        "--apply", "--confirm-processing-disabled", "--diagnostics", str(diagnostic),
    ])
    monkeypatch.setattr(installer, "Installation", lambda path: SimpleNamespace(
        config={"account_id": "fixture-account", "store_key": "merchant"}))
    monkeypatch.setattr(installer, "read_private", lambda path: {"cloudflare_api_token": "fixture-cf-token"})
    monkeypatch.setattr(installer, "validate_backend", lambda *_: ("fixture-db", "merchant-test-commerce"))
    monkeypatch.setattr(installer, "API", lambda *_: SimpleNamespace(
        query=lambda *_: pytest.fail("failed import must not proceed to readback")))
    calls = []
    stderr = "import error: Bearer fixture-cf-token https://upload.example.test/path?signature=private-grant"

    def run(command, **options):
        calls.append(command)
        if timed_out:
            raise subprocess.TimeoutExpired(command, 120, output=b"partial output", stderr=stderr.encode())
        return SimpleNamespace(returncode=1, stdout="partial output", stderr=stderr)

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        installer.main()
    assert error.value.code == 1 and len(calls) == 1
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    saved = diagnostic.read_text()
    records = [json.loads(line) for line in saved.splitlines()]
    assert records[-2]["event"] == ("wrangler-timeout" if timed_out else "wrangler-exit")
    assert records[-2]["stdout"] == "partial output"
    assert records[-2]["stderr"] == "import error: Bearer [REDACTED] https://upload.example.test/path?[REDACTED]"
    assert records[-1]["stage"] == "wrangler-import"
    assert not capsys.readouterr().out
    with pytest.raises(SystemExit):
        installer.main()
    assert len(calls) == 1
    assert diagnostic.read_text() == saved
    assert "no D1 operations were attempted" in capsys.readouterr().err


def test_wrangler_local_sql_file_rolls_back_and_preserves_fence(tmp_path):
    # Exercise the real Wrangler SQL-file parser and workerd D1 with no remote credentials.
    config = tmp_path / "wrangler.json"
    config.write_text(json.dumps({"name": "fence-test", "compatibility_date": "2026-08-31",
                                  "d1_databases": [{"binding": "DB", "database_name": "fence-test",
                                                    "database_id": "00000000-0000-4000-8000-000000000001"}]}))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CLOUDFLARE_", "CF_"))}
    env.update(WRANGLER_SEND_METRICS="false", WRANGLER_LOG_PATH=str(tmp_path / "wrangler.log"))
    sql_file = tmp_path / "fence.sql"

    def execute(flag, value, *, succeeds=True, log_level="log"):
        result = subprocess.run([str(installer.WRANGLER), "d1", "execute", "DB", "--config", str(config),
                                 "--local", "--persist-to", str(tmp_path / "d1"), "--yes", "--json", flag, value],
                                cwd=tmp_path, env={**env, "WRANGLER_LOG": log_level},
                                capture_output=True, text=True, timeout=30)
        if succeeds:
            assert result.returncode == 0, result.stderr or result.stdout
            if log_level != "log":
                assert result.stdout == "" and result.stderr == ""
                return None
            return json.loads(result.stdout)[-1]["results"]
        assert result.returncode != 0

    def run_file(sql, *, succeeds=True):
        sql_file.write_text(sql)
        return execute("--file", str(sql_file), succeeds=succeeds)

    def status():
        return execute("--command", installer.STATUS_SQL.replace("?1", "'merchant'"))[0]

    # The same JSON output path is used for local and remote d1 execute.
    for log_level in ("error", "info"):
        execute("--command", "SELECT 1 AS ok", log_level=log_level)
    assert execute("--command", "SELECT 1 AS ok") == [{"ok": 1}]

    schema = ("CREATE TABLE outbox(id INTEGER PRIMARY KEY); INSERT INTO outbox VALUES (7);"
              "CREATE TABLE orders(order_id TEXT PRIMARY KEY);")
    for migration in ("0006_post_purchase_actions.sql", "0007_post_purchase_checkpoint.sql"):
        schema += "\n" + (ROOT / "migrations" / migration).read_text()
    run_file(schema)
    sql = installer.fence_import_sql("merchant")
    broken = sql[:sql.index("INSERT INTO post_purchase_checkpoint")] + "SELECT json('invalid JSON');"
    run_file(broken, succeeds=False)
    assert installer.classify(status(), "merchant") == "uninitialised"
    run_file(sql)
    first = status()
    assert installer.classify(first, "merchant") == "retained"
    assert first["activation_watermark"] == first["last_outbox_id"] == 7

    execute("--command", "INSERT INTO outbox VALUES (8); UPDATE post_purchase_checkpoint SET last_outbox_id=8;"
            "INSERT INTO orders VALUES ('order'); INSERT INTO post_purchase_executions VALUES "
            "('merchant','test','order','test-workflow','" + ("a" * 64) + "','claimed',NULL,0,0);")
    run_file(sql)
    retained = status()
    assert (retained["activation_watermark"], retained["last_outbox_id"], retained["execution_count"]) == (7, 8, 1)
    run_file(installer.fence_import_sql("foreign"), succeeds=False)
    assert status() == retained
