"""Read a single subject's historical and native publications together.

Historical dates retain their original provenance. Ordering compares calendar days,
not inferred instants, then source-qualified identifiers for a stable total order.
"""

import hashlib
import json
import re

from cartray_reviews.staff_replies import public_replies


def _rows(result):
    return [r.to_py() if hasattr(r, "to_py") else dict(r) for r in result.results]


async def _query(db, sql, *args):
    return _rows(await db.prepare(sql).bind(*args).all())


async def _versions(db, store, environment, subject):
    return await _query(
        db,
        "SELECT p.snapshot_id,v.version,COALESCE(info.public_attribution,'') public_attribution "
        "FROM review_publications p "
        "JOIN review_snapshot_subjects s ON s.store_key=p.store_key AND s.environment=p.environment "
        "AND s.snapshot_id=p.snapshot_id "
        "JOIN review_subjects n ON n.store_key=s.store_key AND n.environment=s.environment "
        "AND n.subject_id=s.subject_id AND n.kind=s.kind AND n.canonical_path=s.canonical_path "
        "JOIN native_publication_versions v ON v.store_key=n.store_key AND v.environment=n.environment "
        "AND v.subject_id=n.subject_id LEFT JOIN review_snapshot_provenance info "
        "ON info.store_key=p.store_key AND info.environment=p.environment AND info.snapshot_id=p.snapshot_id "
        "WHERE p.store_key=? AND p.environment=? AND s.subject_id=?",
        store,
        environment,
        subject,
    )


def _version(row):
    payload = json.dumps([row["snapshot_id"], row["version"]], separators=(",", ":")).encode()
    return "unified-" + hashlib.sha256(payload).hexdigest()


def _historic(row, attribution):
    return {
        "id": "h-" + row["contribution_id"],
        "source_id": row["contribution_id"],
        "kind": row["kind"],
        "source": {"kind": "historical", "attribution": attribution or None},
        "author": row["author"],
        "body": row["body"],
        "rating": row["rating"],
        "date": json.loads(row["source_date"]),
        "title": row["public_title"],
        "verification": row["verification"],
        "anchors": json.loads(row["anchors_json"]),
        "votes": json.loads(row["votes_json"]),
        "replies": [],
    }


async def unified_read(db, store, environment, subject, endpoint, params, response):
    """Read overlapping subject publications with bounded lists and race detection."""

    def fail(status, message):
        return response.json({"error": message}, status=status, headers={"Cache-Control": "no-store"})

    versions = await _versions(db, store, environment, subject)
    if len(versions) != 1:
        return fail(503, "subject conflict")
    snapshot = versions[0]["snapshot_id"]
    attribution = versions[0]["public_attribution"]
    version = _version(versions[0])
    scope = (store, environment, snapshot, subject)
    links = await _query(
        db,
        "SELECT l.source_contribution_id,l.native_review_id FROM review_identity_links l "
        "JOIN review_snapshot_provenance p ON p.store_key=l.store_key AND p.environment=l.environment "
        "AND p.source_identity=l.source_identity WHERE l.store_key=? AND l.environment=? "
        "AND p.snapshot_id=? AND l.subject_id=?",
        *scope,
    )
    linked_ids = {link["source_contribution_id"] for link in links}
    native_links = {link["native_review_id"]: link["source_contribution_id"] for link in links}
    if endpoint == "summary":
        historical = (
            await _query(
                db,
                "SELECT COUNT(*) contribution_count,COUNT(rating) rated_count,COALESCE(SUM(rating),0) rating_sum "
                "FROM review_contributions WHERE store_key=? AND environment=? AND snapshot_id=? AND subject_id=? "
                "AND status='published' AND parent_id IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM review_identity_links l JOIN review_snapshot_provenance p "
                "ON p.store_key=l.store_key AND p.environment=l.environment AND p.source_identity=l.source_identity "
                "WHERE p.store_key=review_contributions.store_key AND p.environment=review_contributions.environment "
                "AND p.snapshot_id=review_contributions.snapshot_id AND l.subject_id=review_contributions.subject_id "
                "AND l.source_contribution_id=review_contributions.contribution_id)",
                *scope,
            )
        )[0]
        native = (
            await _query(
                db,
                "SELECT contribution_count,rated_count,rating_sum FROM native_publication_versions "
                "WHERE store_key=? AND environment=? AND subject_id=?",
                store,
                environment,
                subject,
            )
        )[0]
        count = historical["rated_count"] + native["rated_count"]
        total = historical["rating_sum"] + native["rating_sum"]
        result = {
            "subject_id": subject,
            "snapshot_id": version,
            "rating": {"average": total / count if count else None, "rated_count": count},
            "contribution_count": historical["contribution_count"] + native["contribution_count"],
        }
    else:
        after = params.get("after", [None])[0]
        if after is not None and (
            not re.fullmatch(r"[hn]-[A-Za-z0-9_-]{1,128}", after) or params.get("snapshot", [None])[0] != version
        ):
            return fail(400, "invalid or stale cursor")
        if after is None and "snapshot" in params:
            return fail(400, "invalid cursor")
        # Historical imports have an enforced 200-contribution cap. Native data
        # is paged in SQL using the cursor's day and source-qualified identity.
        historic = await _query(
            db,
            "SELECT contribution_id,COALESCE(parent_id,'') parent_id,kind,author,body,rating,source_date,"
            "verification,anchors_json,"
            "votes_json,COALESCE(sort_day,'') day, (SELECT t.title FROM review_import_titles t "
            "WHERE t.store_key=review_contributions.store_key AND t.environment=review_contributions.environment "
            "AND t.snapshot_id=review_contributions.snapshot_id "
            "AND t.contribution_id=review_contributions.contribution_id) public_title "
            "FROM review_contributions WHERE store_key=? AND environment=? "
            "AND snapshot_id=? AND subject_id=? AND status='published' ORDER BY contribution_id LIMIT 201",
            *scope,
        )
        if len(historic) > 200:
            return fail(503, "historical limit exceeded")
        cursor_day = None
        if after and after.startswith("h-"):
            cursor_day = next(
                (r["day"] for r in historic if not r["parent_id"] and "h-" + r["contribution_id"] == after), None
            )
        elif after:
            found = await _query(
                db,
                "SELECT strftime('%Y-%m-%d',created_at,'unixepoch') day FROM native_reviews WHERE store_key=? "
                "AND environment=? AND subject_id=? AND review_id=? AND state='published'",
                store,
                environment,
                subject,
                after[2:],
            )
            cursor_day = found[0]["day"] if found else None
        if after and cursor_day is None:
            return fail(400, "invalid cursor")
        limit = int(params.get("limit", ["20"])[0])
        native = await _query(
            db,
            "SELECT r.review_id,r.created_at,v.author,v.title,v.body,v.rating, "
            "strftime('%Y-%m-%d',r.created_at,'unixepoch') day, "
            "EXISTS(SELECT 1 FROM native_review_acquisitions a WHERE a.store_key=r.store_key "
            "AND a.environment=r.environment AND a.review_id=r.review_id AND a.subject_id=r.subject_id) verified "
            "FROM native_reviews r JOIN native_revisions v ON v.store_key=r.store_key AND v.environment=r.environment "
            "AND v.revision_id=r.revision_id AND v.review_id=r.review_id "
            "WHERE r.store_key=? AND r.environment=? AND r.subject_id=? AND r.state='published' "
            "AND (? = 0 OR strftime('%Y-%m-%d',r.created_at,'unixepoch') < ? OR "
            "(strftime('%Y-%m-%d',r.created_at,'unixepoch') = ? AND ('n-'||r.review_id)>?)) "
            "ORDER BY day DESC,r.review_id LIMIT ?",
            store,
            environment,
            subject,
            int(cursor_day is not None),
            cursor_day or "",
            cursor_day or "",
            after or "",
            limit + 1,
        )
        roots = []
        for row in historic:
            if not row["parent_id"] and row["contribution_id"] not in linked_ids:
                item = _historic(row, attribution)
                if cursor_day is None or row["day"] < cursor_day or (row["day"] == cursor_day and item["id"] > after):
                    roots.append((row["day"], item))
        for row in native:
            roots.append(
                (
                    row["day"],
                    {
                        "id": "n-" + row["review_id"],
                        "source_id": row["review_id"],
                        "kind": "review",
                        "source": {"kind": "native", "attribution": None},
                        "author": row["author"],
                        "title": row["title"],
                        "body": row["body"],
                        "rating": row["rating"],
                        "created_at": row["created_at"],
                        "verification": "cartray_verified_acquisition" if row["verified"] else "unverified",
                        "replies": [],
                    },
                )
            )
        roots.sort(key=lambda pair: pair[1]["id"])
        roots.sort(key=lambda pair: pair[0], reverse=True)
        items = [item for _, item in roots[:limit]]
        replies = await public_replies(
            db, store, environment, [item["source_id"] for item in items if item["id"].startswith("n-")], unified=True
        )
        for item in items:
            if item["id"].startswith("n-") and item["source_id"] in replies:
                item["replies"].append(replies[item["source_id"]])
        by_id = {item["source_id"]: item for item in items if item["id"].startswith("h-")}
        historical_by_id = {row["contribution_id"]: row for row in historic if not row["parent_id"]}
        for item in items:
            if not item["id"].startswith("n-"):
                continue
            source_id = native_links.get(item["source_id"])
            source = historical_by_id.get(source_id)
            if source is not None:
                legacy = _historic(source, attribution)
                item["historical_provenance"] = {
                    "source_id": source_id,
                    "source": legacy["source"],
                    "date": legacy["date"],
                    "verification": legacy["verification"],
                    "anchors": legacy["anchors"],
                    "votes": legacy["votes"],
                }
                by_id[source_id] = item
        pending = [row for row in historic if row["parent_id"]]
        while pending:
            remaining = []
            for row in pending:
                parent = by_id.get(row["parent_id"])
                if parent is None:
                    remaining.append(row)
                    continue
                child = _historic(row, attribution)
                parent["replies"].append(child)
                by_id[row["contribution_id"]] = child
            if len(remaining) == len(pending):
                break
            pending = remaining
        more = len(roots) > limit
        result = {
            "subject_id": subject,
            "snapshot_id": version,
            "contributions": items,
            "next_after": items[-1]["id"] if more and items else None,
            "has_more": more,
        }
        while len(json.dumps(result, ensure_ascii=True).encode("ascii")) > 128_000:
            items.pop()
            if not items:
                return fail(503, "response exceeds limit")
            result["has_more"] = True
            result["next_after"] = items[-1]["id"]
    latest = await _versions(db, store, environment, subject)
    if len(latest) != 1 or _version(latest[0]) != version:
        return fail(409, "publication changed; restart pagination")
    return response.json(result, headers={"Cache-Control": "no-store"})
