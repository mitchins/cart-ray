"""Test-only issuance from CartRay's confirmed acquisition database."""

import hashlib
import hmac
import json
import re

from .acquisition import confirmed_purchase


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


async def issue_from_settlement(
    reviews_db,
    commerce_db,
    *,
    store,
    environment,
    event_id,
    subject_maps,
    recipient_context_hash,
    token_key,
    now,
    lifetime=604800,
):
    """Issue a deterministic opaque token after reading trusted settlement state.

    The caller owns the trust boundary for both database bindings, the mapping,
    recipient context and the key. This local test adapter does not deliver mail
    or claim that the token bearer is a particular person.
    """
    if not isinstance(recipient_context_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", recipient_context_hash):
        raise ValueError("invalid recipient context")
    if not isinstance(token_key, bytes) or len(token_key) < 32:
        raise ValueError("invalid invitation key")
    if type(now) is not int or type(lifetime) is not int or not 1 <= lifetime <= 604800:
        raise ValueError("invalid invitation lifetime")
    acquisition = await confirmed_purchase(
        commerce_db, store=store, environment=environment, event_id=event_id, subject_maps=subject_maps
    )
    scope = [
        "reviews-acquisition-invitation-v1",
        store,
        environment,
        acquisition.acquisition_id,
        event_id,
        recipient_context_hash,
    ]
    token = hmac.new(token_key, json.dumps(scope, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    subject_hash = _digest(acquisition.subject_ids)
    statements = [
        reviews_db.prepare(
            "INSERT OR IGNORE INTO acquisition_invitations "
            "(store_key,environment,acquisition_id,source_event_id,recipient_context_hash,subject_set_hash,"
            "token_hash,expires_at,key_version) VALUES (?,?,?,?,?,?,?,?, 'v1')"
        ).bind(
            store,
            environment,
            acquisition.acquisition_id,
            event_id,
            recipient_context_hash,
            subject_hash,
            token_hash,
            now + lifetime,
        ),
        reviews_db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM acquisition_invitations "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND source_event_id=? "
            "AND recipient_context_hash=? AND subject_set_hash=? AND token_hash=? "
            "AND revoked_at IS NULL AND expires_at>?) THEN 1 ELSE 0 END"
        ).bind(
            store,
            environment,
            acquisition.acquisition_id,
            event_id,
            recipient_context_hash,
            subject_hash,
            token_hash,
            now,
        ),
    ]
    for subject in acquisition.subject_ids:
        statements.extend(
            [
                reviews_db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_subjects "
                    "WHERE store_key=? AND environment=? AND subject_id=?) THEN 1 ELSE 0 END"
                ).bind(store, environment, subject),
                reviews_db.prepare(
                    "INSERT OR IGNORE INTO acquisition_invitation_subjects "
                    "(store_key,environment,acquisition_id,subject_id) VALUES (?,?,?,?)"
                ).bind(store, environment, acquisition.acquisition_id, subject),
            ]
        )
    statements.append(reviews_db.prepare("DELETE FROM native_guard"))
    try:
        await reviews_db.batch(statements)
    except Exception as exc:
        raise ValueError("invitation unavailable") from exc
    record = _rows(
        await reviews_db.prepare(
            "SELECT expires_at FROM acquisition_invitations WHERE store_key=? AND environment=? AND acquisition_id=?"
        )
        .bind(store, environment, acquisition.acquisition_id)
        .all()
    )
    if len(record) != 1:
        raise ValueError("invitation unavailable")
    return {"token": token, "expires_at": record[0]["expires_at"], "subjects": acquisition.subject_ids}


async def redeem_purchase(
    reviews_db,
    *,
    store,
    environment,
    subject,
    token,
    request_key,
    author,
    title,
    body,
    rating,
    reviewer_key_secret,
    now,
    random_token,
):
    """Atomically consume one eligible subject and create a pending native review."""
    if environment != "test" or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invitation unavailable")
    if not isinstance(reviewer_key_secret, bytes) or len(reviewer_key_secret) < 32:
        raise ValueError("reviewer key unavailable")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    payload_hash = hashlib.sha256(
        json.dumps([subject, author, title, body, rating], ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    rows = _rows(
        await reviews_db.prepare(
            "SELECT i.acquisition_id FROM acquisition_invitations i "
            "JOIN acquisition_invitation_subjects s ON s.store_key=i.store_key AND s.environment=i.environment "
            "AND s.acquisition_id=i.acquisition_id WHERE i.store_key=? AND i.environment=? "
            "AND i.token_hash=? AND s.subject_id=? AND i.revoked_at IS NULL AND i.expires_at>? "
            "AND i.expires_at>unixepoch()"
        )
        .bind(store, environment, token_hash, subject, now)
        .all()
    )
    if len(rows) != 1:
        raise ValueError("invitation unavailable")
    acquisition_id = rows[0]["acquisition_id"]
    existing = _rows(
        await reviews_db.prepare(
            "SELECT payload_hash,token_hash,review_id,revision_id FROM native_requests "
            "WHERE store_key=? AND environment=? AND request_key=?"
        )
        .bind(store, environment, request_key)
        .all()
    )
    if existing:
        record = existing[0]
        if record["payload_hash"] != payload_hash or record["token_hash"] != token_hash:
            raise ValueError("request conflict")
        return {"review_id": record["review_id"], "revision_id": record["revision_id"], "status": "pending"}
    reviewer = hmac.new(
        reviewer_key_secret,
        json.dumps(
            ["reviews-acquisition-reviewer-v1", store, environment, acquisition_id], separators=(",", ":")
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    review_id, revision_id, event_id = random_token(), random_token(), random_token()
    statements = [
        reviews_db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM acquisition_invitations i "
            "JOIN acquisition_invitation_subjects s ON s.store_key=i.store_key AND s.environment=i.environment "
            "AND s.acquisition_id=i.acquisition_id WHERE i.store_key=? AND i.environment=? AND i.token_hash=? "
            "AND i.revoked_at IS NULL AND i.expires_at>? AND i.expires_at>unixepoch() "
            "AND s.subject_id=? AND s.consumed_at IS NULL) "
            "THEN 1 ELSE 0 END"
        ).bind(store, environment, token_hash, now, subject),
        reviews_db.prepare("INSERT INTO native_reviews VALUES (?,?,?,?,?,'pending',?,?)").bind(
            store, environment, review_id, subject, reviewer, revision_id, now
        ),
        reviews_db.prepare(
            "INSERT INTO native_revisions VALUES (?,?,?,?,?,NULLIF(?,''),?,CAST(NULLIF(?,'') AS INTEGER))"
        ).bind(
            store,
            environment,
            revision_id,
            review_id,
            author,
            title or "",
            body,
            str(rating) if rating is not None else "",
        ),
        reviews_db.prepare("INSERT INTO native_requests VALUES (?,?,?,?,?,?,?)").bind(
            store, environment, request_key, payload_hash, token_hash, review_id, revision_id
        ),
        reviews_db.prepare(
            "UPDATE acquisition_invitation_subjects SET consumed_at=? "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND subject_id=? AND consumed_at IS NULL"
        ).bind(now, store, environment, acquisition_id, subject),
        reviews_db.prepare("INSERT INTO native_review_acquisitions VALUES (?,?,?,?,?)").bind(
            store, environment, review_id, acquisition_id, subject
        ),
        reviews_db.prepare("INSERT INTO native_audit VALUES (?,?,?,?,?,?,?,?)").bind(
            event_id, store, environment, review_id, revision_id, "submitted", "acquisition_capability", now
        ),
        reviews_db.prepare("DELETE FROM native_guard"),
    ]
    try:
        await reviews_db.batch(statements)
    except Exception as exc:
        # A concurrent identical retry may have committed first; it is safe to
        # return only its existing result, never to create another review.
        existing = _rows(
            await reviews_db.prepare(
                "SELECT payload_hash,token_hash,review_id,revision_id FROM native_requests "
                "WHERE store_key=? AND environment=? AND request_key=?"
            )
            .bind(store, environment, request_key)
            .all()
        )
        if existing and existing[0]["payload_hash"] == payload_hash and existing[0]["token_hash"] == token_hash:
            return {
                "review_id": existing[0]["review_id"],
                "revision_id": existing[0]["revision_id"],
                "status": "pending",
            }
        raise ValueError("invitation unavailable") from exc
    return {"review_id": review_id, "revision_id": revision_id, "status": "pending"}


async def revoke_purchase(reviews_db, *, store, environment, token, now):
    """Revoke a scoped invitation; existing published reviews are unchanged."""
    if environment != "test" or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invitation unavailable")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        await reviews_db.batch(
            [
                reviews_db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM acquisition_invitations "
                    "WHERE store_key=? AND environment=? AND token_hash=?) THEN 1 ELSE 0 END"
                ).bind(store, environment, token_hash),
                reviews_db.prepare(
                    "UPDATE acquisition_invitations SET revoked_at=COALESCE(revoked_at,?) "
                    "WHERE store_key=? AND environment=? AND token_hash=?"
                ).bind(now, store, environment, token_hash),
                reviews_db.prepare("DELETE FROM native_guard"),
            ]
        )
    except Exception as exc:
        raise ValueError("invitation unavailable") from exc
