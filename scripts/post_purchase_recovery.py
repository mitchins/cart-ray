#!/usr/bin/env python3
"""Inspect or disposition interrupted test-only post-purchase executions via operator Wrangler."""

import argparse
import os
import re
import subprocess

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "hold"))
    parser.add_argument("--account", required=True, help="Cloudflare test account ID")
    parser.add_argument("--database", required=True, help="explicit test D1 name")
    parser.add_argument("--merchant", required=True)
    parser.add_argument("--offset", type=int, default=0, help="inspection page offset (100 records per page)")
    parser.add_argument("--local", action="store_true", help="use the local Wrangler D1 instead of remote")
    parser.add_argument("--order")
    parser.add_argument("--workflow")
    parser.add_argument("--digest", help="action digest observed during inspection")
    parser.add_argument("--operator", help="operator identity for the audit record")
    parser.add_argument("--evidence", help="brief provider-receipt investigation and reason; no secrets")
    parser.add_argument("--confirm-worker-paused", action="store_true")
    args = parser.parse_args()
    if (not re.fullmatch(r"[a-f0-9]{32}", args.account)
            or not re.fullmatch(r"[a-z0-9-]{1,63}", args.database)
            or "test" not in args.database.split("-")
            or not IDENTIFIER.fullmatch(args.merchant) or not 0 <= args.offset <= 1_000_000):
        parser.error("a test database, account and merchant identity are required")
    merchant = literal(args.merchant)
    if args.action == "inspect":
        sql = (
            "SELECT order_id,workflow_key,action_digest,state,provider_reference,created_at,updated_at,"
            "CAST(strftime('%s','now') AS INTEGER)-updated_at AS age_seconds "
            "FROM post_purchase_executions WHERE merchant_id=" + merchant
            + " AND environment='test' AND state IN ('claimed','uncertain') "
            "ORDER BY CASE state WHEN 'claimed' THEN 0 ELSE 1 END,updated_at,order_id,workflow_key "
            f"LIMIT 100 OFFSET {args.offset};"
        )
    else:
        if (not args.confirm_worker_paused or not args.order or not IDENTIFIER.fullmatch(args.order)
                or not args.workflow or not IDENTIFIER.fullmatch(args.workflow)
                or not args.digest or not re.fullmatch(r"[a-f0-9]{64}", args.digest)
                or not args.operator or not 1 <= len(args.operator) <= 128
                or not args.evidence or not 1 <= len(args.evidence) <= 512
                or any(ord(c) < 32 for c in args.operator + args.evidence)):
            parser.error("hold requires a paused Worker, inspected identity, operator and evidence")
        sql = (
            "INSERT INTO post_purchase_recovery_audit "
            "(merchant_id,environment,order_id,workflow_key,action_digest,operator,evidence,recorded_at) VALUES ("
            + ",".join(map(literal, (args.merchant, "test", args.order, args.workflow, args.digest,
                                      args.operator, args.evidence)))
            + ",CAST(strftime('%s','now') AS INTEGER));"
        )
    env = os.environ | {"CLOUDFLARE_ACCOUNT_ID": args.account}
    subprocess.run(
        ["npx", "--no-install", "wrangler", "d1", "execute", args.database,
         "--local" if args.local else "--remote", "--json", "--command", sql],
        env=env, check=True,
    )


if __name__ == "__main__":
    main()
