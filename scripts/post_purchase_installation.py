#!/usr/bin/env python3
"""Initialise a test installation's independent post-purchase fence; dry-run by default.

Uses the existing protected operator credentials and Wrangler remote D1 binding.
Never enables sending, provisions a handler, resets claims, or repairs partial state.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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

# D1 batch() rolls back the entire sequence if this assertion or either insert fails.
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

BRIDGE = """
import {getPlatformProxy} from 'wrangler';
let raw='';for await(const part of process.stdin){raw+=part;if(raw.length>32768)throw Error('input limit');}
const input=JSON.parse(raw);
const platform=await getPlatformProxy({configPath:input.config,envFiles:[],persist:false,remoteBindings:true});
try {
 const result=await platform.env.DB.batch(input.statements.map(item=>{
  const statement=platform.env.DB.prepare(item.sql);
  return item.params.length?statement.bind(...item.params):statement;
 }));
 if(result.some(item=>item.success!==true))throw Error('D1 batch failed');
 process.stdout.write(JSON.stringify(result.at(-1).results[0]));
} finally {await platform.dispose();}
"""


def statements(merchant, apply=False):
    if not isinstance(merchant, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", merchant):
        raise ValueError("invalid merchant identity")
    writes = [{"sql": sql, "params": [merchant]} for sql in (GUARD_SQL, INSTALL_SQL, CHECKPOINT_SQL)]
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


def remote_batch(account, database, name, token, queries):
    with tempfile.TemporaryDirectory(prefix="cartray-pp-install-") as directory:
        config = Path(directory) / "wrangler.json"
        config.write_text(json.dumps({
            "name": "cartray-pp-operator-local", "account_id": account,
            "compatibility_date": "2026-08-31",
            "d1_databases": [{"binding": "DB", "database_id": database, "database_name": name, "remote": True}],
        }))
        result = subprocess.run(
            ["node", "--input-type=module", "-e", BRIDGE], cwd=ROOT,
            input=json.dumps({"config": str(config), "statements": queries}), capture_output=True, text=True,
            env={**os.environ, "CLOUDFLARE_API_TOKEN": token, "WRANGLER_LOG": "error"}, timeout=120,
        )
        if result.returncode:
            # Provider diagnostics can contain private context; never echo captured streams.
            raise RuntimeError("post-purchase D1 batch failed; state not repaired; inspect then retry")
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise RuntimeError("post-purchase D1 batch returned no receipt; inspect then retry")
        return json.loads(lines[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="retained test installation inventory")
    parser.add_argument("--credentials", type=Path, required=True, help="existing protected operator credential JSON")
    parser.add_argument("--apply", action="store_true", help="explicitly initialise the atomic no-backfill fence")
    parser.add_argument("--confirm-processing-disabled", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.confirm_processing_disabled:
        parser.error("--apply requires --confirm-processing-disabled after prior drains have stopped")
    try:
        installation = Installation(args.directory)
        merchant = installation.config.get("store_key")
        queries = statements(merchant, args.apply)
        credentials = read_private(args.credentials)
        token = credentials.get("cloudflare_api_token")
        if not isinstance(token, str) or not token:
            raise ValueError("credential handoff requires cloudflare_api_token")
        api = API(installation.config["account_id"], token)
        database, name = validate_backend(installation, api)
        status = remote_batch(installation.config["account_id"], database, name, token, queries)
        state = classify(status, merchant)
        if args.apply and state != "retained":
            raise RuntimeError("initialisation receipt is incomplete; inspect then retry")
        print(json.dumps({"merchant": merchant, "environment": "test", "applied": args.apply,
                          "state": state, "sendingEnabled": False, **status}, sort_keys=True))
    except (ValueError, RuntimeError, CloudError, subprocess.TimeoutExpired):
        parser.exit(1, "Post-purchase initialisation refused or outcome unknown; inspect scope/schema and retry; "
                    "no sending was enabled.\n")


if __name__ == "__main__":
    main()
