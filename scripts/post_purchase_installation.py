#!/usr/bin/env python3
"""Initialise a test installation's independent post-purchase fence; dry-run by default.

Reads status through the D1 API. Explicit apply uses one transactional D1 SQL-file import.
Never enables sending, provisions a handler, resets claims, or repairs partial state.
"""

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
WRANGLER = ROOT / "node_modules/.bin/wrangler"
DIAGNOSTICS_DIR = ".cartray-operator-diagnostics"
sys.path.insert(0, str(ROOT / "modules/reviews/operator"))
from cloud import API, CloudError  # noqa: E402
from operate import Installation, read_private  # noqa: E402

STATUS_SQL = """SELECT
 (SELECT COUNT(*) FROM post_purchase_installation) AS installation_count,
 (SELECT merchant_id FROM post_purchase_installation WHERE singleton=1) AS installation_merchant,
 (SELECT environment FROM post_purchase_installation WHERE singleton=1) AS installation_environment,
 (SELECT COUNT(*) FROM post_purchase_checkpoint) AS checkpoint_count,
 (SELECT merchant_id FROM post_purchase_checkpoint WHERE singleton=1) AS checkpoint_merchant,
 (SELECT environment FROM post_purchase_checkpoint WHERE singleton=1) AS checkpoint_environment,
 (SELECT activation_watermark FROM post_purchase_checkpoint WHERE singleton=1) AS activation_watermark,
 (SELECT last_outbox_id FROM post_purchase_checkpoint WHERE singleton=1) AS last_outbox_id,
 (SELECT COALESCE(MAX(id),0) FROM outbox) AS current_outbox_id,
 (SELECT COUNT(*) FROM post_purchase_executions) AS execution_count,
 (SELECT COUNT(*) FROM post_purchase_executions
  WHERE merchant_id IS NOT ?1 OR environment IS NOT 'test') AS foreign_execution_count"""

# The D1 SQL-file import rolls back the sequence if this assertion or either insert fails.
# The SQL-time guard closes the read/dry-run-to-write race, including half-pairs.
GUARD_SQL = """SELECT json(CASE WHEN (
 ((SELECT COUNT(*) FROM post_purchase_installation)=0
  AND (SELECT COUNT(*) FROM post_purchase_checkpoint)=0
  AND (SELECT COUNT(*) FROM post_purchase_executions)=0)
 OR ((SELECT COUNT(*) FROM post_purchase_installation)=1
  AND (SELECT COUNT(*) FROM post_purchase_checkpoint)=1
  AND NOT EXISTS (SELECT 1 FROM post_purchase_executions
    WHERE merchant_id IS NOT ?1 OR environment IS NOT 'test')
  AND EXISTS (SELECT 1 FROM post_purchase_installation i
    JOIN post_purchase_checkpoint c ON i.singleton=c.singleton
    WHERE i.singleton=1 AND i.merchant_id=?1 AND i.environment='test'
      AND c.merchant_id=i.merchant_id AND c.environment=i.environment
      AND c.activation_watermark>=0 AND c.last_outbox_id>=c.activation_watermark
      AND c.last_outbox_id<=(SELECT COALESCE(MAX(id),0) FROM outbox)))
) THEN 'true' ELSE 'invalid post-purchase installation scope/checkpoint' END)"""

INSTALL_SQL = """INSERT INTO post_purchase_installation(singleton,merchant_id,environment)
 SELECT 1,?1,'test' WHERE NOT EXISTS (SELECT 1 FROM post_purchase_installation)"""
CHECKPOINT_SQL = """INSERT INTO post_purchase_checkpoint
 (singleton,merchant_id,environment,activation_watermark,last_outbox_id)
 SELECT 1,?1,'test',COALESCE(MAX(id),0),COALESCE(MAX(id),0) FROM outbox
 HAVING NOT EXISTS (SELECT 1 FROM post_purchase_checkpoint)"""
FENCE_WRITES = (GUARD_SQL, INSTALL_SQL, CHECKPOINT_SQL)


def statements(merchant, apply=False):
    if not isinstance(merchant, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", merchant):
        raise ValueError("invalid merchant identity")
    writes = [{"sql": sql, "params": [merchant]} for sql in FENCE_WRITES]
    return (writes if apply else []) + [{"sql": STATUS_SQL, "params": [merchant]}]


def classify(status, merchant):
    if status["installation_count"] == status["checkpoint_count"] == status["execution_count"] == 0:
        return "uninitialised"
    if (status["installation_count"] == status["checkpoint_count"] == 1
            and status["foreign_execution_count"] == 0
            and status["installation_merchant"] == status["checkpoint_merchant"] == merchant
            and status["installation_environment"] == status["checkpoint_environment"] == "test"
            and type(status["activation_watermark"]) is int and type(status["last_outbox_id"]) is int
            and 0 <= status["activation_watermark"] <= status["last_outbox_id"] <= status["current_outbox_id"]):
        return "retained"
    raise ValueError("post-purchase installation scope/checkpoint mismatch; no repair or rewind permitted")


def validate_backend(installation, api):
    database = installation.db("commerce")
    name = installation.name("commerce")
    if "test" not in name.split("-") or api.request("d1/database/" + database).get("name") != name:
        raise ValueError("test database identity mismatch")
    actual = api.version(installation.name("source"))
    if actual["id"] != installation.state.get("deployment", {}).get("versions", {}).get("source"):
        raise ValueError("source deployment differs from inventory")
    bindings = {b["name"]: b for b in actual["resources"]["bindings"]}
    mode = bindings.get("CARTRAY_POST_PURCHASE_MODE")
    if (bindings.get("DB", {}).get("type") != "d1"
            or bindings.get("DB", {}).get("database_id") != database
            or bindings.get("CARTRAY_ENVIRONMENT", {}).get("type") != "plain_text"
            or bindings.get("CARTRAY_ENVIRONMENT", {}).get("text") != "test"
            or (mode is not None and (mode.get("type") != "plain_text" or mode.get("text") != "disabled"))):
        raise ValueError("source must bind this test database with post-purchase processing disabled")
    return database, name


def fence_import_sql(merchant):
    *writes, _status = statements(merchant, apply=True)
    # SQL-file imports have no bind parameters; statements() restricts the literal
    # to ASCII identifier characters and only the fixed fence SQL is rendered.
    return "".join(query["sql"].replace("?1", f"'{merchant}'") + ";\n" for query in writes)


def open_diagnostics():
    uid = os.geteuid()
    home = Path(pwd.getpwuid(uid).pw_dir)
    if not home.is_absolute():
        raise ValueError("operator account home must be absolute")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    home_fd = os.open(home, directory_flags)
    try:
        home_stat = os.fstat(home_fd)
        if home_stat.st_uid != uid or home_stat.st_mode & 0o022:
            raise ValueError("operator account home must not be writable by other users")
        try:
            os.mkdir(DIAGNOSTICS_DIR, 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(DIAGNOSTICS_DIR, directory_flags, dir_fd=home_fd)
        try:
            directory_stat = os.fstat(directory_fd)
            if directory_stat.st_uid != uid or stat.S_IMODE(directory_stat.st_mode) != 0o700:
                raise ValueError("diagnostic directory must be owned by the operator and mode 0700")
            name = f"post-purchase-{uuid4().hex}.jsonl"
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(home_fd)
    return os.fdopen(fd, "w", encoding="utf-8"), home / DIAGNOSTICS_DIR / name


def diagnostic_text(value, token):
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    if isinstance(token, str) and token:
        value = value.replace(token, "[REDACTED]")
    value = re.sub(r"(?i)\bBearer\s+[\w.~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r'''(https?://[^\s"'<>?]+)\?[^\s"'<>]+''', r"\1?[REDACTED]", value)
    return value if len(value) <= 16384 else value[:8192] + "\n[truncated]\n" + value[-8192:]


def import_receipt_json(stdout):
    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate completion JSON key")
        return value

    def invalid_constant(_value):
        raise ValueError("invalid completion JSON constant")

    lines = stdout.splitlines(keepends=True)
    for index, line in enumerate(lines):
        # The non-interactive import spinner writes these lines even with --json.
        progress = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
        if not progress or progress == "\u2502" or progress.startswith(("\u251c ", "\u2502 ")):
            continue
        # Decode the entire tail so two receipts or trailing output cannot pass.
        return json.loads("".join(lines[index:]), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    raise ValueError("missing completion JSON")


def remote_import(account, database, name, token, merchant, diagnostics=None):
    sql = fence_import_sql(merchant)
    with tempfile.TemporaryDirectory(prefix="cartray-pp-install-") as directory:
        config = Path(directory) / "wrangler.json"
        fence = Path(directory) / "fence.sql"
        config.write_text(json.dumps({
            "name": "cartray-pp-operator-local", "account_id": account,
            "compatibility_date": "2026-08-31",
            "d1_databases": [{"binding": "DB", "database_id": database, "database_name": name}],
        }))
        fence.write_text(sql, encoding="ascii")
        # --file uses the transactional D1 import API; --command and getPlatformProxy
        # use different transports. A failed import does not leave a partial fence.
        # https://developers.cloudflare.com/d1/get-started/#5-deploy-your-application
        try:
            result = subprocess.run(
                [str(WRANGLER), "d1", "execute", "DB", "--config", str(config), "--remote",
                 "--file", str(fence), "--yes", "--json"],
                cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
                # Wrangler restores this level before emitting the final JSON at log level.
                env={**os.environ, "CLOUDFLARE_API_TOKEN": token, "WRANGLER_LOG": "log",
                     "WRANGLER_LOG_PATH": str(Path(directory) / "wrangler.log"), "WRANGLER_LOG_SANITIZE": "true",
                     "WRANGLER_SEND_METRICS": "false"},
            )
        except subprocess.TimeoutExpired as error:
            if diagnostics:
                diagnostics("wrangler-timeout", stdout=error.stdout or b"", stderr=error.stderr or b"")
            raise
        if diagnostics:
            diagnostics("wrangler-exit", returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if result.returncode:
            # Provider diagnostics can contain private context; never echo captured streams.
            raise RuntimeError("post-purchase D1 import failed; inspect then retry")
        try:
            receipt = import_receipt_json(result.stdout)
        except ValueError as error:
            raise RuntimeError("post-purchase D1 import returned empty or invalid completion JSON") from error
        result = receipt[0] if isinstance(receipt, list) and len(receipt) == 1 else None
        bookmark = result.get("finalBookmark") if isinstance(result, dict) else None
        summary = result.get("results") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("success") is not True
                or not isinstance(bookmark, str) or not bookmark
                or not isinstance(summary, list) or len(summary) != 1 or not isinstance(summary[0], dict)
                or summary[0].get("Total queries executed") != len(FENCE_WRITES)):
            raise RuntimeError("post-purchase D1 import returned no completion receipt; inspect then retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="retained test installation inventory")
    parser.add_argument("--credentials", type=Path, required=True, help="existing protected operator credential JSON")
    parser.add_argument("--apply", action="store_true", help="explicitly initialise the atomic no-backfill fence")
    parser.add_argument("--confirm-processing-disabled", action="store_true")
    parser.add_argument("--diagnostics", action="store_true",
                        help=f"create a redacted 0600 receipt under the operator account home ({DIAGNOSTICS_DIR}/)")
    args = parser.parse_args()
    if args.apply and not args.confirm_processing_disabled:
        parser.error("--apply requires --confirm-processing-disabled after prior drains have stopped")
    stream = None
    token = None
    stage = "diagnostic-setup"

    def record(event, **details):
        if stream is not None:
            data = {key: diagnostic_text(value, token) if isinstance(value, (str, bytes)) else value
                    for key, value in details.items()}
            stream.write(json.dumps({"event": event, **data}) + "\n")
            stream.flush()

    try:
        if args.diagnostics:
            stream, path = open_diagnostics()
            print(f"Protected diagnostics: {path}", file=sys.stderr)
        stage = "inventory-and-credentials"
        record(stage, apply=args.apply)
        installation = Installation(args.directory)
        merchant = installation.config.get("store_key")
        query, = statements(merchant)
        credentials = read_private(args.credentials)
        token = credentials.get("cloudflare_api_token")
        if not isinstance(token, str) or not token:
            raise ValueError("credential handoff requires cloudflare_api_token")
        api = API(installation.config["account_id"], token)
        stage = "backend-validation"
        record(stage)
        database, name = validate_backend(installation, api)
        if args.apply:
            stage = "wrangler-import"
            record(stage)
            remote_import(installation.config["account_id"], database, name, token, merchant, diagnostics=record)
        stage = "status-readback"
        record(stage)
        rows = api.query(database, query["sql"], query["params"])
        if len(rows) != 1:
            raise RuntimeError("post-purchase fence status returned no unique receipt")
        status, = rows
        state = classify(status, merchant)
        if args.apply and state != "retained":
            raise RuntimeError("initialisation receipt is incomplete; inspect then retry")
        record("complete", state=state, activation_watermark=status["activation_watermark"],
               last_outbox_id=status["last_outbox_id"], execution_count=status["execution_count"])
        print(json.dumps({"merchant": merchant, "environment": "test", "applied": args.apply,
                          "state": state, "sendingEnabled": False, **status}, sort_keys=True))
    except (ValueError, RuntimeError, CloudError, OSError, subprocess.TimeoutExpired) as error:
        if stream is not None:
            try:
                record("failed", stage=stage, error_type=type(error).__name__, error=str(error))
            except OSError:
                pass
        if stage == "diagnostic-setup":
            parser.exit(1, "Cannot create protected diagnostic file; no D1 operations were attempted.\n")
        if args.apply:
            parser.exit(1, "Post-purchase initialisation refused or outcome unknown; inspect scope/schema and retry; "
                        "no sending was enabled.\n")
        parser.exit(1, "Post-purchase status read refused or unavailable; inspect scope/schema and retry; "
                    "no D1 writes were attempted and no sending was enabled.\n")
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    main()
