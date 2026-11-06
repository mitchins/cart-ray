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
    provenance = (
        store,
        environment,
        sid,
        source["identity"],
        source["extracted_at"],
        source["sha256"],
        1,
        source.get("public_attribution"),
    )
    lines.append(
        "INSERT INTO review_import_guard SELECT CASE WHEN NOT EXISTS("
        "SELECT 1 FROM review_publications p JOIN review_snapshot_provenance s "
        "ON s.store_key=p.store_key AND s.environment=p.environment AND s.snapshot_id=p.snapshot_id "
        f"WHERE p.store_key={st} AND p.environment={env} AND s.source_identity<>{literal(source['identity'])}) "
        "THEN 1 ELSE 0 END;"
    )
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
        lines.append(
            "INSERT INTO review_import_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM review_identity_links "
            f"WHERE store_key={st} AND environment={env} AND source_identity={literal(source['identity'])} "
            f"AND source_contribution_id={literal(item['id'])} AND "
            f"(subject_id<>{literal(item['subject_id'])} OR source_revision_id<>{literal(item['revision_id'])} "
            f"OR {literal(item['parent_id'])} IS NOT NULL)) "
            "THEN 1 ELSE 0 END;"
        )
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
    # Preserve linked history while its native review can still be published.
    # A withdrawn native review is a permanent tombstone, so its old source
    # root and descendants may then be removed from a newer snapshot.
    lines.append(
        "INSERT INTO review_import_guard WITH RECURSIVE keep(id) AS ("
        "SELECT l.source_contribution_id FROM review_identity_links l "
        f"WHERE l.store_key={st} AND l.environment={env} AND l.source_identity={literal(source['identity'])} "
        "AND NOT EXISTS(SELECT 1 FROM native_reviews n WHERE n.store_key=l.store_key "
        "AND n.environment=l.environment AND n.review_id=l.native_review_id "
        "AND n.subject_id=l.subject_id AND n.state='withdrawn') "
        "UNION SELECT c.contribution_id FROM review_contributions c JOIN keep k ON c.parent_id=k.id "
        "JOIN review_publications p ON p.store_key=c.store_key AND p.environment=c.environment "
        "AND p.snapshot_id=c.snapshot_id "
        f"WHERE c.store_key={st} AND c.environment={env}) "
        "SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM keep k "
        "JOIN review_publications p ON p.store_key=" + st + " AND p.environment=" + env + " "
        "JOIN review_contributions old ON old.store_key=p.store_key AND old.environment=p.environment "
        "AND old.snapshot_id=p.snapshot_id AND old.contribution_id=k.id "
        "LEFT JOIN review_contributions new ON new.store_key=old.store_key AND new.environment=old.environment "
        f"AND new.snapshot_id={snapshot_key} AND new.contribution_id=old.contribution_id "
        "WHERE new.contribution_id IS NULL OR new.subject_id IS NOT old.subject_id "
        "OR new.parent_id IS NOT old.parent_id OR new.revision_id IS NOT old.revision_id "
        "OR new.author IS NOT old.author OR new.body IS NOT old.body OR new.rating IS NOT old.rating "
        "OR new.source_date IS NOT old.source_date OR new.verification IS NOT old.verification "
        "OR new.anchors_json IS NOT old.anchors_json OR new.votes_json IS NOT old.votes_json "
        "OR (SELECT title FROM review_import_titles WHERE store_key=old.store_key AND environment=old.environment "
        "AND snapshot_id=old.snapshot_id AND contribution_id=old.contribution_id) IS NOT "
        "(SELECT title FROM review_import_titles WHERE store_key=new.store_key AND environment=new.environment "
        "AND snapshot_id=new.snapshot_id AND contribution_id=new.contribution_id)) THEN 1 ELSE 0 END;"
    )
    # A withdrawn root suppresses all descendants, even across later snapshots.
    # Search every retained snapshot for that source instance so omission cannot
    # erase ancestry and permit a reply to reappear under another parent.
    lines.append(
        "INSERT INTO review_import_guard WITH RECURSIVE hidden(id) AS ("
        "SELECT c.contribution_id FROM review_identity_links l JOIN native_reviews n "
        "ON n.store_key=l.store_key AND n.environment=l.environment AND n.review_id=l.native_review_id "
        "AND n.subject_id=l.subject_id AND n.state='withdrawn' "
        "JOIN review_snapshot_provenance p ON p.store_key=l.store_key AND p.environment=l.environment "
        "AND p.source_identity=l.source_identity "
        "JOIN review_contributions c ON c.store_key=p.store_key AND c.environment=p.environment "
        "AND c.snapshot_id=p.snapshot_id AND c.subject_id=l.subject_id AND c.parent_id=l.source_contribution_id "
        f"WHERE l.store_key={st} AND l.environment={env} AND l.source_identity={literal(source['identity'])} "
        "UNION SELECT c.contribution_id FROM hidden h JOIN review_contributions c ON c.parent_id=h.id "
        "JOIN review_snapshot_provenance p ON p.store_key=c.store_key AND p.environment=c.environment "
        "AND p.snapshot_id=c.snapshot_id "
        f"WHERE c.store_key={st} AND c.environment={env} AND p.source_identity={literal(source['identity'])}) "
        "SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM hidden h JOIN review_contributions c "
        "ON c.contribution_id=h.id "
        f"WHERE c.store_key={st} AND c.environment={env} AND c.snapshot_id={snapshot_key} "
        "AND NOT EXISTS(SELECT 1 FROM review_publications current "
        f"WHERE current.store_key={st} AND current.environment={env} AND current.snapshot_id={snapshot_key})) "
        "THEN 1 ELSE 0 END;"
    )
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
