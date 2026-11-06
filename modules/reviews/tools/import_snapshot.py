"""Import an independently pinned, approved snapshot into local Reviews D1."""

import argparse
import json
import tempfile
from datetime import date
from pathlib import Path

from cartray_reviews.snapshot import load_snapshot
from install import execute


def literal(value):
    """Encode data as SQLite text literals without interpreting SQL syntax."""
    if value is None:
        return "NULL"
    if type(value) is int:
        return str(value)
    return "CAST(X'" + str(value).encode("utf-8").hex() + "' AS TEXT)"


def sort_day(source_date):
    """Use the source calendar day when unambiguous; unknown dates sort last."""
    if source_date["precision"] == "unknown":
        return None
    original = source_date["original"]
    try:
        day = original[:10]
        if len(day) != 10 or date.fromisoformat(day).isoformat() != day:
            return None
        return day
    except ValueError:
        return None


def import_snapshot(data, *, digest, store, environment):
    """Validate fully and use one atomic D1 batch for content and pointer."""
    snapshot = load_snapshot(data, expected_sha256=digest, store=store, environment=environment)
    if len(snapshot["contributions"]) > 200 or len(snapshot["subjects"]) > 200:
        raise ValueError("snapshot exceeds local import batch limit")
    if len(json.dumps(snapshot, ensure_ascii=True).encode("ascii")) > 90_000:
        raise ValueError("snapshot exceeds read-only response budget")
    sid, rev = snapshot["snapshot_id"], snapshot["publication_revision"]
    scoped = ",".join(map(literal, (store, environment, sid)))
    st, env, snapshot_key, fingerprint = map(literal, (store, environment, sid, digest))
    # The guard aborts the entire D1 batch on stale or conflicting identity.
    lines = [
        "INSERT INTO review_import_guard SELECT CASE WHEN "
        f"NOT EXISTS(SELECT 1 FROM review_snapshots WHERE store_key={st} AND environment={env} "
        f"AND ((snapshot_id={snapshot_key} AND digest<>{fingerprint}) "
        f"OR (revision={rev} AND snapshot_id<>{snapshot_key}))) "
        f"AND NOT EXISTS(SELECT 1 FROM review_publications WHERE store_key={st} "
        f"AND environment={env} AND revision>{rev}) "
        "THEN 1 ELSE 0 END;",
        f"INSERT OR IGNORE INTO review_snapshots VALUES ({scoped},{rev},{fingerprint});",
    ]
    source = snapshot["source"]
    provenance = (store, environment, sid, source["identity"], source["extracted_at"], source["sha256"], 1)
    lines.append(
        "INSERT OR IGNORE INTO review_snapshot_provenance VALUES (" + ",".join(map(literal, provenance)) + ");"
    )
    for subject in snapshot["subjects"]:
        lines.append(
            "INSERT INTO review_import_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM review_subjects "
            f"WHERE store_key={st} AND environment={env} AND subject_id={literal(subject['id'])} "
            f"AND (kind<>{literal(subject['kind'])} OR canonical_path<>{literal(subject['path'])})) "
            "THEN 1 ELSE 0 END;"
        )
        values = (store, environment, sid, subject["id"], subject["kind"], subject["path"])
        lines.append("INSERT OR IGNORE INTO review_snapshot_subjects VALUES (" + ",".join(map(literal, values)) + ");")
    for item in snapshot["contributions"]:
        values = (
            store,
            environment,
            sid,
            item["id"],
            item["revision_id"],
            item["subject_id"],
            item["parent_id"],
            item["kind"],
            "published",
            item["author"],
            item["body"],
            item["rating"],
            json.dumps(item["date"], separators=(",", ":")),
            sort_day(item["date"]),
            item["verification"],
            json.dumps(item["anchors"], separators=(",", ":")),
            json.dumps(item["votes"], separators=(",", ":")),
        )
        lines.append("INSERT OR IGNORE INTO review_contributions VALUES (" + ",".join(map(literal, values)) + ");")
        title = item.get("title")
        if title is not None:
            values = (store, environment, sid, item["id"], title)
            lines.append("INSERT OR IGNORE INTO review_import_titles VALUES (" + ",".join(map(literal, values)) + ");")
    lines += [
        f"INSERT INTO review_publications VALUES ({scoped},{rev}) ON CONFLICT(store_key,environment) "
        "DO UPDATE SET snapshot_id=excluded.snapshot_id,revision=excluded.revision;",
        "DELETE FROM review_import_guard;",
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql") as file:
        file.write("\n".join(lines))
        file.flush()
        execute("--file", file.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--environment", choices=["test"], required=True)
    args = parser.parse_args()
    import_snapshot(args.snapshot.read_bytes(), digest=args.sha256, store=args.store, environment=args.environment)
    print("Approved snapshot imported into local Reviews D1")
