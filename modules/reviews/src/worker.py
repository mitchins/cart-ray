"""Opt-in, read-only Reviews Worker. No write or scheduled entry points."""

import json
import re
import time
from urllib.parse import parse_qs, urlparse

from cartray_reviews.automation_initial import unsubscribe
from cartray_reviews.lookup import resolve_subject
from cartray_reviews.unified import unified_read
from native_lifecycle import (
    delivery_queue_read,
    discard_body,
    handle_write,
    native_read,
    pending_read,
    provider_evidence_staff_read,
    published_staff_read,
    read_json,
    staff_history_read,
)
from workers import Response, WorkerEntrypoint


def rows(result):
    """Convert D1 result rows into Python dictionaries."""
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def subject_sources(db, store, environment, subject_id):
    """Read both source memberships and versions in a single D1 statement."""
    return rows(
        await db.prepare(
            "SELECT s.subject_id, s.snapshot_id, 0 native_version, 'imported' source_kind, "
            "COALESCE(info.public_attribution,'') source_attribution "
            "FROM review_snapshot_subjects s JOIN review_publications p "
            "ON p.store_key=s.store_key AND p.environment=s.environment AND p.snapshot_id=s.snapshot_id "
            "LEFT JOIN review_snapshot_provenance info ON info.store_key=s.store_key "
            "AND info.environment=s.environment AND info.snapshot_id=s.snapshot_id "
            "WHERE s.store_key=? AND s.environment=? AND s.subject_id=? UNION ALL "
            "SELECT s.subject_id,'' snapshot_id,v.version native_version,'native' source_kind,'' source_attribution "
            "FROM review_subjects s JOIN native_publication_versions v "
            "ON v.store_key=s.store_key AND v.environment=s.environment AND v.subject_id=s.subject_id "
            "WHERE s.store_key=? AND s.environment=? AND s.subject_id=? LIMIT 2"
        )
        .bind(store, environment, subject_id, store, environment, subject_id)
        .all()
    )


async def publication_unchanged(db, store, environment, subject_id, expected):
    """Detect changed versions and either source appearing or disappearing."""
    current = await subject_sources(db, store, environment, subject_id)
    return sorted((r["source_kind"], r["snapshot_id"], r["native_version"]) for r in current) == sorted(
        (r["source_kind"], r["snapshot_id"], r["native_version"]) for r in expected
    )


def public_contribution(row, attribution):
    """Construct an explicit public allowlist from a persisted contribution."""
    return {
        "id": row["contribution_id"],
        "kind": row["kind"],
        "source": {"kind": "historical", "attribution": attribution or None},
        "author": row["author"],
        "body": row["body"],
        "title": row["public_title"],
        "rating": row["rating"],
        "date": json.loads(row["source_date"]),
        "verification": row["verification"],
        "anchors": json.loads(row["anchors_json"]),
        "votes": json.loads(row["votes_json"]),
    }


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        mode = str(getattr(self.env, "REVIEWS_MODE", "disabled"))
        url = urlparse(str(request.url))
        if url.path == "/reviews/v1/invitation-unsubscribe":
            if str(request.method) != "POST" or url.query:
                return Response.json({"error": "method not allowed"}, status=405)
            key = str(getattr(self.env, "REVIEWS_UNSUBSCRIBE_KEY", ""))
            excluded = [
                str(getattr(self.env, name, ""))
                for name in (
                    "REVIEWS_INVITATION_KEY",
                    "REVIEWS_CONSENT_SECRET",
                    "REVIEWS_AUTOMATION_INGRESS_TOKEN",
                    "REVIEWS_TEST_ADMIN_TOKEN",
                    "REVIEWS_SETTLEMENT_TOKEN",
                    "REVIEWS_REVIEWER_KEY",
                    "REVIEWS_DELIVERY_TOKEN",
                    "REVIEWS_RECIPIENT_EVIDENCE_SECRET",
                    "REVIEWS_AUTOMATION_ACTIVATION_TOKEN",
                )
            ]
            if (
                str(getattr(self.env, "REVIEWS_ENVIRONMENT", "")) != "test"
                or len(key) < 32
                or key in excluded
                or not getattr(self.env, "REVIEWS_DB", None)
            ):
                await discard_body(request)
                return Response.json({"error": "unavailable"}, status=503)
            try:
                data = await read_json(request)
                if set(data) != {"token"}:
                    raise ValueError("invalid")
                await unsubscribe(
                    self.env.REVIEWS_DB,
                    store=str(self.env.REVIEWS_STORE_KEY),
                    environment="test",
                    token=data["token"],
                    secret=key.encode(),
                    now=int(time.time()),
                )
                return Response.json({"status": "unsubscribed"}, headers={"Cache-Control": "no-store"})
            except (ValueError, TypeError, AttributeError, KeyError):
                return Response.json({"error": "unavailable"}, status=409)
        if mode not in ("read_only", "collect", "verified_acquisition_only"):
            if str(request.method) != "GET":
                await discard_body(request)
            return Response.json({"error": "not found"}, status=404)
        if str(request.method) == "GET" and url.path == "/reviews-admin/v1/pending":
            if mode not in ("collect", "verified_acquisition_only"):
                return Response.json({"error": "not found"}, status=404)
            return await pending_read(request, self.env, url.query)
        if str(request.method) == "GET" and url.path == "/reviews-admin/v1/published":
            if mode not in ("collect", "verified_acquisition_only"):
                return Response.json({"error": "not found"}, status=404)
            return await published_staff_read(request, self.env, url.query)
        if str(request.method) == "GET" and url.path == "/reviews-admin/v1/deliveries":
            if mode not in ("collect", "verified_acquisition_only"):
                return Response.json({"error": "not found"}, status=404)
            return await delivery_queue_read(request, self.env, url.query)
        delivery_evidence = re.fullmatch(r"/reviews-admin/v1/deliveries/([0-9a-f]{64})/provider-evidence", url.path)
        if str(request.method) == "GET" and delivery_evidence:
            if mode not in ("collect", "verified_acquisition_only"):
                return Response.json({"error": "not found"}, status=404)
            return await provider_evidence_staff_read(request, self.env, url.query, delivery_evidence.group(1))
        staff_history = re.fullmatch(
            r"/reviews-admin/v1/contributions/([0-9a-f]{64})/(revisions|audit|reply-revisions|reply-audit)", url.path
        )
        if str(request.method) == "GET" and staff_history:
            if mode not in ("collect", "verified_acquisition_only"):
                return Response.json({"error": "not found"}, status=404)
            return await staff_history_read(request, self.env, url.query, *staff_history.groups())
        if str(request.method) != "GET":
            if mode not in ("collect", "verified_acquisition_only") or url.query:
                await discard_body(request)
                return Response.json({"error": "method not allowed"}, status=405)
            return await handle_write(request, self.env, url.path)
        if mode in ("collect", "verified_acquisition_only") and str(self.env.REVIEWS_ENVIRONMENT) != "test":
            return Response.json({"error": "collection unavailable"}, status=503)
        query_limit = 6500 if url.path == "/reviews/v1/subject-lookup" else 512
        if len(url.query) > query_limit:
            return Response.json({"error": "invalid query"}, status=400)
        store, environment = str(self.env.REVIEWS_STORE_KEY), str(self.env.REVIEWS_ENVIRONMENT)
        if not store or environment not in ("test", "live"):
            return Response.json({"error": "invalid configuration"}, status=503)
        if url.path == "/reviews/v1/subject-lookup":
            return await resolve_subject(self.env.REVIEWS_DB, store, environment, url.query, Response)
        match = re.fullmatch(r"/reviews/v1/subjects/([A-Za-z0-9_-]{1,128})/(summary|contributions)", url.path)
        if not match:
            return Response.json({"error": "not found"}, status=404)
        params = parse_qs(url.query, keep_blank_values=True)
        if set(params) - {"limit", "after", "snapshot"} or any(len(v) != 1 for v in params.values()):
            return Response.json({"error": "invalid query"}, status=400)
        try:
            limit = int(params.get("limit", ["20"])[0])
        except ValueError:
            return Response.json({"error": "invalid limit"}, status=400)
        if not 1 <= limit <= 50:
            return Response.json({"error": "invalid limit"}, status=400)
        subject_id, endpoint = match.groups()
        db = self.env.REVIEWS_DB
        subject = await subject_sources(db, store, environment, subject_id)
        if not subject:
            return Response.json({"error": "not found"}, status=404)
        if len(subject) == 2:
            # Cross-source identity reconciliation is not yet qualified for live data.
            if environment != "test":
                return Response.json({"error": "publication unavailable"}, status=503)
            return await unified_read(db, store, environment, subject_id, endpoint, params, Response)
        if subject[0]["source_kind"] == "native":
            response = await native_read(
                db, store, environment, subject_id, endpoint, params, subject[0]["native_version"]
            )
            if not await publication_unchanged(db, store, environment, subject_id, subject):
                return Response.json(
                    {"error": "publication changed; restart"}, status=409, headers={"Cache-Control": "no-store"}
                )
            return response
        snapshot_id = subject[0]["snapshot_id"]
        after = params.get("after", [None])[0]
        cursor_snapshot = params.get("snapshot", [None])[0]
        if after is not None and (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", after) or cursor_snapshot != snapshot_id):
            return Response.json({"error": "invalid or stale cursor"}, status=400)
        if after is None and cursor_snapshot is not None:
            return Response.json({"error": "invalid cursor"}, status=400)
        scope = (store, environment, snapshot_id, subject_id)
        if endpoint == "summary":
            result = rows(
                await db.prepare(
                    "SELECT COUNT(*) contribution_count, COUNT(c.rating) rated_count, SUM(c.rating) rating_sum "
                    "FROM review_contributions c WHERE c.store_key=? AND c.environment=? "
                    "AND c.snapshot_id=? AND c.subject_id=? "
                    "AND c.status='published' AND c.parent_id IS NULL"
                )
                .bind(*scope)
                .all()
            )[0]
            count, total = result["rated_count"], result["rating_sum"]
            if not await publication_unchanged(db, store, environment, subject_id, subject):
                return Response.json(
                    {"error": "publication changed; restart"}, status=409, headers={"Cache-Control": "no-store"}
                )
            return Response.json(
                {
                    "subject_id": subject_id,
                    "snapshot_id": snapshot_id,
                    "rating": {"average": total / count if count else None, "rated_count": count},
                    "contribution_count": result["contribution_count"],
                },
                headers={"Cache-Control": "no-store"},
            )
        cursor_day = "\uffff"
        cursor_id = ""
        if after:
            cursor = rows(
                await db.prepare(
                    "SELECT COALESCE(sort_day,'') sort_day FROM review_contributions "
                    "WHERE store_key=? AND environment=? AND snapshot_id=? AND subject_id=? "
                    "AND contribution_id=? AND status='published' AND parent_id IS NULL"
                )
                .bind(*scope, after)
                .all()
            )
            if not cursor:
                return Response.json({"error": "invalid cursor"}, status=400)
            cursor_day, cursor_id = cursor[0]["sort_day"], after
        result = rows(
            await db.prepare(
                "SELECT contribution_id,parent_id,kind,author,body,rating,source_date,verification,"
                "anchors_json,votes_json, (SELECT t.title FROM review_import_titles t "
                "WHERE t.store_key=review_contributions.store_key AND t.environment=review_contributions.environment "
                "AND t.snapshot_id=review_contributions.snapshot_id AND "
                "t.contribution_id=review_contributions.contribution_id) public_title "
                "FROM review_contributions WHERE store_key=? AND environment=? AND snapshot_id=? AND subject_id=? "
                "AND status='published' AND parent_id IS NULL AND "
                "(COALESCE(sort_day,'') < ? OR (COALESCE(sort_day,'') = ? AND contribution_id > ?)) "
                "ORDER BY COALESCE(sort_day,'') DESC, contribution_id LIMIT ?"
            )
            .bind(*scope, cursor_day, cursor_day, cursor_id, limit + 1)
            .all()
        )
        reply_rows = rows(
            await db.prepare(
                "SELECT contribution_id,parent_id,kind,author,body,rating,source_date,verification,"
                "anchors_json,votes_json, (SELECT t.title FROM review_import_titles t "
                "WHERE t.store_key=review_contributions.store_key AND t.environment=review_contributions.environment "
                "AND t.snapshot_id=review_contributions.snapshot_id AND "
                "t.contribution_id=review_contributions.contribution_id) public_title "
                "FROM review_contributions WHERE store_key=? AND environment=? AND snapshot_id=? AND subject_id=? "
                "AND status='published' AND parent_id IS NOT NULL ORDER BY sort_day,contribution_id LIMIT 201"
            )
            .bind(*scope)
            .all()
        )
        if len(reply_rows) > 200:
            return Response.json({"error": "reply limit exceeded"}, status=503)
        attribution = subject[0]["source_attribution"]
        roots = [public_contribution(x, attribution) | {"replies": []} for x in result[:limit]]
        by_id = {x["id"]: x for x in roots}
        pending = list(reply_rows)
        while pending:
            remaining = []
            for row in pending:
                parent = by_id.get(row["parent_id"])
                if parent is None:
                    remaining.append(row)
                    continue
                child = public_contribution(row, attribution) | {"replies": []}
                parent["replies"].append(child)
                by_id[child["id"]] = child
            if len(remaining) == len(pending):
                break
            pending = remaining
        if not await publication_unchanged(db, store, environment, subject_id, subject):
            return Response.json(
                {"error": "publication changed; restart"}, status=409, headers={"Cache-Control": "no-store"}
            )
        return Response.json(
            {
                "subject_id": subject_id,
                "snapshot_id": snapshot_id,
                "contributions": roots,
                "next_after": roots[-1]["id"] if len(result) > limit and roots else None,
                "has_more": len(result) > limit,
            },
            headers={"Cache-Control": "no-store"},
        )
