"""Explicit synthetic REST-batch qualification on an approved, empty disposable D1.

Prepare/review this tool freely. Running it mutates the named database and requires
separate owner approval. It never provisions, deletes or binds cloud resources.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
ROOT = MODULE.parents[1]
sys.path.insert(0, str(MODULE / "operator"))
sys.path.insert(0, str(MODULE / "tests"))
from cloud import API, CloudError  # noqa: E402
from installed_snapshot import save_receipt  # noqa: E402
from installed_snapshot_workerd import batch, publication, statements  # noqa: E402
from operate import read_private  # noqa: E402

TABLES = (
    "review_snapshots", "review_snapshot_provenance", "review_snapshot_subjects", "review_contributions",
    "review_import_titles", "review_subject_aliases", "review_import_guard", "review_publications",
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()



def payload_receipt(sql):
    """Fingerprint the JSON bytes serialized by the existing API.request transport."""
    body = json.dumps({"batch": [{"sql": item} for item in sql]}).encode()
    return {"sha256": hashlib.sha256(body).hexdigest(), "statements": len(sql)}


def target_arguments(account, database, database_name, acknowledged):
    if (not acknowledged or not re.fullmatch(r"[a-f0-9]{32}", account)
            or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", database)
            or not re.fullmatch(r"cartray-snapshot-proof-[a-z0-9-]{1,40}", database_name)):
        raise ValueError("requires explicit disposable acknowledgement, account, D1 UUID and proof-only name")


def credentials(path):
    value = read_private(path)
    if (not isinstance(value, dict) or set(value) != {"cloudflare_api_token"}
            or not isinstance(value["cloudflare_api_token"], str) or not value["cloudflare_api_token"]):
        raise ValueError("qualification credential JSON must contain only cloudflare_api_token")
    return value["cloudflare_api_token"]


def assert_empty_target(api, database, database_name):
    actual = api.request("d1/database/" + database)
    if (not isinstance(actual, dict) or actual.get("uuid") != database or actual.get("name") != database_name):
        raise ValueError("actual D1 resource identity differs from the approved disposable target")
    tables = api.query(database, "SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name NOT GLOB 'sqlite_*' AND name NOT GLOB '_cf_*' ORDER BY name")
    if tables:
        raise ValueError("target is not empty; refuse migrations or synthetic qualification on an existing database")


def state(api, database):
    value = {}
    for table in TABLES:
        rows = api.query(database, "SELECT * FROM " + table)
        value[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return fingerprint(value)


def execute(api, database, operation, payloads, name):
    # Exact production API.batch wire format, not Wrangler's SQL-file import.
    sql = operation["sql"]
    payloads[name] = payload_receipt(sql)
    result = api.batch(database, sql)
    if not result or result[-1] != [operation["expected"]]:
        raise ValueError("qualification readback identity/counts differ: " + name)


def rejected(api, database, operation, payloads, name):
    before = state(api, database)
    try:
        execute(api, database, operation, payloads, name)
    except CloudError as error:
        # An unavailable API also leaves unchanged state: it is not rollback proof.
        # The provider transport must positively classify a SQLite constraint.
        expected = ("CHECK constraint failed: ok = 1", "qualification: late publication failure")
        messages = [item.get("message", "") for item in getattr(error, "provider_errors", ())
                    if isinstance(item, dict)]
        if not any(token in message for message in messages for token in expected):
            raise ValueError("inconclusive rejection (not a classified SQLite constraint): " + name) from error
    else:
        raise ValueError("expected qualification batch was accepted: " + name)
    after = state(api, database)
    payloads[name].update(before_state_sha256=before, after_state_sha256=after,
                          expected_sql_constraint=True)
    if after != before:
        raise ValueError("REST batch was not atomic; retained database requires inspection: " + name)


def qualify(api, database, database_name, receipt_path):
    if receipt_path.suffix != ".json" or receipt_path.exists():
        raise ValueError("supply a new .json receipt path; existing receipts are never overwritten")
    assert_empty_target(api, database, database_name)
    fixture_path = MODULE / "tests/fixtures/approved-synthetic.json"
    original = json.loads(fixture_path.read_bytes())
    newer = original | {"snapshot_id": "two", "publication_revision": 2}
    conflict = json.loads(json.dumps(original))
    conflict["contributions"][0]["body"] = "Synthetic conflicting content"
    same_revision = original | {"snapshot_id": "other-two", "publication_revision": 2}
    late = original | {"snapshot_id": "three", "publication_revision": 3}
    ignored = original | {"snapshot_id": "ignored-three", "publication_revision": 3}
    payloads, results = {}, {}
    receipt = {
        "format": 1, "operation": "synthetic-installed-snapshot-REST-qualification", "status": "HOLD",
        "target": {"account_id": api.account, "database_id": database, "database_name": database_name},
        "fixture_id": "approved-synthetic.json",
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "fixture_variants": {name: publication(value) for name, value in
                             (("original", original), ("newer", newer), ("conflict", conflict),
                              ("same_revision", same_revision), ("late", late), ("ignored", ignored))},
        "started_at": int(time.time()), "payloads": payloads, "results": results,
    }
    try:
        manifest = json.loads((MODULE / "module.json").read_text())
        for migration in manifest["migrations"]:
            sql = statements((MODULE / migration).read_text())
            payloads[migration] = payload_receipt(sql)
            api.batch(database, sql)
        execute(api, database, batch(original), payloads, "original")
        first = state(api, database)
        execute(api, database, batch(original), payloads, "replay")
        if state(api, database) != first:
            raise ValueError("exact replay changed durable state")
        results["replay_idempotent"] = True
        rejected(api, database, batch(conflict, publication(original)), payloads, "conflicting_digest")
        rejected(api, database, batch(newer), payloads, "wrong_predecessor")
        execute(api, database, batch(newer, publication(original)), payloads, "newer")
        rejected(api, database, batch(original), payloads, "stale_revision")
        rejected(api, database, batch(same_revision, publication(newer)), payloads, "revision_collision")
        results["stale_conflict_predecessor_rejected"] = True
        api.batch(database, ["CREATE TRIGGER qualification_silent_skip BEFORE INSERT ON review_contributions "
                            "WHEN NEW.snapshot_id='ignored-three' AND NEW.contribution_id='r' "
                            "BEGIN SELECT RAISE(IGNORE); END;"])
        rejected(api, database, batch(ignored, publication(newer)), payloads, "silent_skip_count_guard")
        api.batch(database, ["DROP TRIGGER qualification_silent_skip;"])
        results["silent_skip_count_guard_rollback"] = True
        api.batch(database, ["CREATE TRIGGER qualification_late_failure BEFORE UPDATE ON review_publications "
                            "BEGIN SELECT RAISE(ABORT, 'qualification: late publication failure'); END;"])
        rejected(api, database, batch(late, publication(newer)), payloads, "late_publication_failure")
        api.batch(database, ["DROP TRIGGER qualification_late_failure;"])
        results["late_failure_rollback"] = True
        execute(api, database, batch(late, publication(newer)), payloads, "retry_after_failure")
        results["retry_after_failure"] = True
        receipt["final_state_sha256"] = state(api, database)
        receipt["final_readback"] = batch(late, publication(newer))["expected"]
        receipt["status"] = "PASS"
    except (CloudError, ValueError) as error:
        receipt["failure"] = str(error)
        raise
    finally:
        receipt["finished_at"] = int(time.time())
        save_receipt(receipt_path.parent, receipt_path.stem, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ack-disposable", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        target_arguments(args.account, args.database, args.database_name, args.ack_disposable)
        api = API(args.account, credentials(args.credentials))
        receipt = qualify(api, args.database, args.database_name, args.receipt)
    except (CloudError, ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
