"""Resolve an explicit subject identity or pinned historical alias."""

import re
from urllib.parse import parse_qs


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def _matches(db, store, environment, alias):
    # The branch selects a fixed column name, never caller-provided SQL.
    column = "canonical_path" if alias.startswith("/") else "subject_id"
    result = (
        await db.prepare(
            "SELECT s.subject_id,s.kind,s.canonical_path FROM review_subject_aliases a "
            "JOIN review_publications p ON p.store_key=a.store_key AND p.environment=a.environment "
            "AND p.snapshot_id=a.snapshot_id JOIN review_snapshot_subjects s ON s.store_key=a.store_key "
            "AND s.environment=a.environment AND s.snapshot_id=a.snapshot_id AND s.subject_id=a.subject_id "
            "WHERE a.store_key=? AND a.environment=? AND a.alias=? UNION ALL "
            "SELECT s.subject_id,s.kind,s.canonical_path FROM review_snapshot_subjects s JOIN review_publications p "
            "ON p.store_key=s.store_key AND p.environment=s.environment AND p.snapshot_id=s.snapshot_id "
            f"WHERE s.store_key=? AND s.environment=? AND s.{column}=? UNION ALL "
            "SELECT subject_id,kind,canonical_path FROM review_subjects "
            f"WHERE store_key=? AND environment=? AND {column}=? LIMIT 4"
        )
        .bind(store, environment, alias, store, environment, alias, store, environment, alias)
        .all()
    )
    return sorted({(row["subject_id"], row["kind"], row["canonical_path"]) for row in _rows(result)})


async def resolve_subject(db, store, environment, query, response):
    """Return one canonical subject, failing closed on ambiguity or races."""

    def fail(status, message):
        return response.json({"error": message}, status=status, headers={"Cache-Control": "no-store"})

    params = parse_qs(query, keep_blank_values=True)
    if set(params) != {"alias"} or len(params["alias"]) != 1:
        return fail(400, "invalid alias")
    alias = params["alias"][0]
    if len(alias) > 2048 or not (
        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", alias) or re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", alias)
    ):
        return fail(400, "invalid alias")
    first = await _matches(db, store, environment, alias)
    if len(first) > 1:
        return fail(409, "ambiguous subject")
    if not first:
        return fail(404, "not found")
    if await _matches(db, store, environment, alias) != first:
        return fail(409, "publication changed; retry")
    identity, kind, path = first[0]
    return response.json(
        {"subject_id": identity, "kind": kind, "canonical_path": path}, headers={"Cache-Control": "no-store"}
    )
