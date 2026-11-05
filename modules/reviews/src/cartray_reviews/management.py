"""Scoped native review management, using immutable revisions and guarded writes."""

import hashlib
import hmac
import json
import re


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


def management_token(secret, *, store, environment, review_id):
    """Derive a scoped capability from the random review identity and server key."""
    if (
        not isinstance(secret, bytes)
        or len(secret) < 32
        or not isinstance(review_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", review_id)
    ):
        raise ValueError("management unavailable")
    scope = json.dumps(["reviews-management-v1", store, environment, review_id], separators=(",", ":")).encode()
    return hmac.new(secret, scope, hashlib.sha256).hexdigest()


async def manage_review(
    db, *, store, environment, review_id, capability, secret, action, revision, data, request_key, now, random_token
):
    """Edit or withdraw one acquisition-backed review with replay-safe writes."""
    if (
        environment != "test"
        or action not in ("edit", "withdraw")
        or not isinstance(capability, str)
        or not re.fullmatch(r"[0-9a-f]{64}", capability)
        or not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-f]{64}", revision)
        or not isinstance(request_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_key)
    ):
        raise ValueError("management unavailable")
    expected = management_token(secret, store=store, environment=environment, review_id=review_id)
    if not hmac.compare_digest(expected, capability):
        raise ValueError("management unavailable")
    if action == "withdraw":
        if not isinstance(data, dict) or data:
            raise ValueError("invalid update")
    else:
        if not isinstance(data, dict) or set(data) - {"author", "title", "body", "rating"}:
            raise ValueError("invalid update")
        author, title, body, rating = (
            data.get("author", ""),
            data.get("title"),
            data.get("body", ""),
            data.get("rating"),
        )
        if (
            not isinstance(author, str)
            or len(author.strip()) > 120
            or (title is not None and (not isinstance(title, str) or len(title) > 200))
            or not isinstance(body, str)
            or len(body) > 5000
            or (rating is not None and (type(rating) is not int or not 1 <= rating <= 5))
            or (rating is None and len(body.strip()) < 3)
        ):
            raise ValueError("invalid update")
    digest = hashlib.sha256(
        json.dumps(
            [review_id, action, revision, data], ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    existing = _rows(
        await db.prepare(
            "SELECT review_id,payload_hash,resulting_revision,resulting_state FROM native_management_requests "
            "WHERE store_key=? AND environment=? AND request_key=?"
        )
        .bind(store, environment, request_key)
        .all()
    )
    if existing:
        item = existing[0]
        if item["review_id"] != review_id or item["payload_hash"] != digest:
            raise ValueError("management conflict")
        return {"review_id": review_id, "revision_id": item["resulting_revision"], "status": item["resulting_state"]}
    current = _rows(
        await db.prepare(
            "SELECT r.state,r.subject_id,r.revision_id FROM native_reviews r JOIN native_review_acquisitions a "
            "ON a.store_key=r.store_key AND a.environment=r.environment AND a.review_id=r.review_id "
            "WHERE r.store_key=? AND r.environment=? AND r.review_id=?"
        )
        .bind(store, environment, review_id)
        .all()
    )
    if (
        len(current) != 1
        or current[0]["revision_id"] != revision
        or current[0]["state"] not in ("pending", "published", "rejected")
    ):
        raise ValueError("management unavailable")
    state, subject = current[0]["state"], current[0]["subject_id"]
    new_revision = random_token() if action == "edit" else revision
    event = random_token()
    desired = "pending" if action == "edit" else "withdrawn"
    statements = [
        db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM native_reviews r "
            "JOIN native_review_acquisitions a ON a.store_key=r.store_key AND a.environment=r.environment "
            "AND a.review_id=r.review_id JOIN native_publication_versions p ON p.store_key=r.store_key "
            "AND p.environment=r.environment AND p.subject_id=r.subject_id "
            "JOIN native_revisions v ON v.store_key=r.store_key AND v.environment=r.environment "
            "AND v.revision_id=r.revision_id AND v.review_id=r.review_id "
            "WHERE r.store_key=? AND r.environment=? AND r.review_id=? "
            "AND r.revision_id=? AND r.state=?) THEN 1 ELSE 0 END"
        ).bind(store, environment, review_id, revision, state)
    ]
    if action == "edit":
        statements.append(
            db.prepare(
                "INSERT INTO native_revisions VALUES (?,?,?,?,?,NULLIF(?,''),?,CAST(NULLIF(?,'') AS INTEGER))"
            ).bind(
                store,
                environment,
                new_revision,
                review_id,
                author.strip() or "Anonymous",
                title or "",
                body,
                str(rating) if rating is not None else "",
            )
        )
    if state == "published":
        statements.extend(
            [
                db.prepare(
                    "UPDATE native_publication_versions SET version=version+1, "
                    "contribution_count=contribution_count-1, "
                    "rated_count=rated_count-(SELECT CASE WHEN rating IS NULL THEN 0 ELSE 1 END FROM native_revisions "
                    "WHERE store_key=? AND environment=? AND revision_id=?), rating_sum=rating_sum-"
                    "(SELECT COALESCE(rating,0) FROM native_revisions WHERE store_key=? AND environment=? "
                    "AND revision_id=?) "
                    "WHERE store_key=? AND environment=? AND subject_id=?"
                ).bind(store, environment, revision, store, environment, revision, store, environment, subject),
                db.prepare("INSERT INTO native_effect_outbox VALUES (?,?,?,?,?,?)").bind(
                    event, store, environment, subject, "invalidate", now
                ),
            ]
        )
    statements.extend(
        [
            db.prepare(
                "UPDATE native_reviews SET state=?,revision_id=? WHERE store_key=? AND environment=? "
                "AND review_id=? AND revision_id=? AND state=?"
            ).bind(desired, new_revision, store, environment, review_id, revision, state),
            db.prepare("INSERT INTO native_audit VALUES (?,?,?,?,?,?,?,?)").bind(
                event,
                store,
                environment,
                review_id,
                new_revision,
                "edited" if action == "edit" else "withdrawn",
                "reviewer_capability",
                now,
            ),
            db.prepare("INSERT INTO native_management_requests VALUES (?,?,?,?,?,?,?,?)").bind(
                store, environment, request_key, review_id, digest, action, new_revision, desired
            ),
            db.prepare("DELETE FROM native_guard"),
        ]
    )
    try:
        await db.batch(statements)
    except Exception as exc:
        previous = _rows(
            await db.prepare(
                "SELECT review_id,payload_hash,resulting_revision,resulting_state FROM native_management_requests "
                "WHERE store_key=? AND environment=? AND request_key=?"
            )
            .bind(store, environment, request_key)
            .all()
        )
        if previous and previous[0]["review_id"] == review_id and previous[0]["payload_hash"] == digest:
            return {
                "review_id": review_id,
                "revision_id": previous[0]["resulting_revision"],
                "status": previous[0]["resulting_state"],
            }
        raise ValueError("management unavailable") from exc
    return {"review_id": review_id, "revision_id": new_revision, "status": desired}
