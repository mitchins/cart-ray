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


async def record_signed_consent(
    reviews_db, commerce_db, *, store, environment, data, signature, secret, subject_maps, now
):
    """Record purpose-bound evidence from an independently authenticated adapter."""
    fields = {"event_id", "recipient_context_hash", "evidence_reference", "granted_at"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("consent unavailable")
    recipient = data["recipient_context_hash"]
    evidence = data["evidence_reference"]
    granted = data["granted_at"]
    if (
        not isinstance(secret, bytes)
        or len(secret) < 32
        or not isinstance(signature, str)
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
        or not isinstance(recipient, str)
        or not re.fullmatch(r"[0-9a-f]{64}", recipient)
        or not isinstance(evidence, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", evidence)
        or type(granted) is not int
        or granted < 0
        or granted > now
    ):
        raise ValueError("consent unavailable")
    signed = json.dumps(
        ["reviews-consent-v1", store, environment, data], ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    if not hmac.compare_digest(hmac.new(secret, signed, hashlib.sha256).hexdigest(), signature):
        raise ValueError("consent unavailable")
    acquisition = await confirmed_purchase(
        commerce_db, store=store, environment=environment, event_id=data["event_id"], subject_maps=subject_maps
    )
    try:
        await reviews_db.batch(
            [
                reviews_db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN NOT EXISTS("
                    "SELECT 1 FROM review_invitation_consent_withdrawals WHERE store_key=? AND environment=? "
                    "AND acquisition_id=? AND recipient_context_hash=? AND purpose='review_invitation') "
                    "THEN 1 ELSE 0 END"
                ).bind(store, environment, acquisition.acquisition_id, recipient),
                reviews_db.prepare(
                    "INSERT OR IGNORE INTO review_invitation_consents VALUES (?,?,?,?,'review_invitation',?,?,NULL)"
                ).bind(store, environment, acquisition.acquisition_id, recipient, evidence, granted),
                reviews_db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_invitation_consents "
                    "WHERE store_key=? AND environment=? AND acquisition_id=? AND recipient_context_hash=? "
                    "AND purpose='review_invitation' AND evidence_reference=? AND granted_at=? "
                    "AND revoked_at IS NULL) THEN 1 ELSE 0 END"
                ).bind(store, environment, acquisition.acquisition_id, recipient, evidence, granted),
                reviews_db.prepare("DELETE FROM native_guard"),
            ]
        )
    except Exception as exc:
        raise ValueError("consent unavailable") from exc


async def withdraw_signed_consent(
    reviews_db, commerce_db, *, store, environment, data, signature, secret, subject_maps, now
):
    """Apply independently authenticated purpose-specific suppression, never grant consent."""
    fields = {"event_id", "recipient_context_hash", "evidence_reference", "withdrawn_at"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("consent unavailable")
    recipient, evidence, withdrawn = data["recipient_context_hash"], data["evidence_reference"], data["withdrawn_at"]
    if (
        not isinstance(secret, bytes)
        or len(secret) < 32
        or not isinstance(signature, str)
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
        or not isinstance(recipient, str)
        or not re.fullmatch(r"[0-9a-f]{64}", recipient)
        or not isinstance(evidence, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", evidence)
        or type(withdrawn) is not int
        or type(now) is not int
        or not 0 < withdrawn <= now
    ):
        raise ValueError("consent unavailable")
    signed = json.dumps(
        ["reviews-consent-withdrawal-v1", store, environment, data],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if not hmac.compare_digest(hmac.new(secret, signed, hashlib.sha256).hexdigest(), signature):
        raise ValueError("consent unavailable")
    acquisition = await confirmed_purchase(
        commerce_db, store=store, environment=environment, event_id=data["event_id"], subject_maps=subject_maps
    )
    try:
        await reviews_db.batch(
            [
                reviews_db.prepare(
                    "INSERT OR IGNORE INTO review_invitation_consent_withdrawals "
                    "VALUES (?,?,?,?,'review_invitation',?,?)"
                ).bind(store, environment, acquisition.acquisition_id, recipient, evidence, withdrawn),
                reviews_db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM "
                    "review_invitation_consent_withdrawals WHERE store_key=? AND environment=? "
                    "AND acquisition_id=? AND recipient_context_hash=? AND purpose='review_invitation' "
                    "AND evidence_reference=? AND withdrawn_at=?) THEN 1 ELSE 0 END"
                ).bind(store, environment, acquisition.acquisition_id, recipient, evidence, withdrawn),
                reviews_db.prepare(
                    "UPDATE review_invitation_consents SET revoked_at=CASE "
                    "WHEN revoked_at IS NULL OR revoked_at>? THEN ? ELSE revoked_at END "
                    "WHERE store_key=? AND environment=? AND acquisition_id=? AND recipient_context_hash=? "
                    "AND purpose='review_invitation'"
                ).bind(withdrawn, withdrawn, store, environment, acquisition.acquisition_id, recipient),
                reviews_db.prepare("DELETE FROM native_guard"),
            ]
        )
    except Exception as exc:
        raise ValueError("consent unavailable") from exc


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
    lifetime=60 * 86400,
    pending=False,
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
    if (
        type(now) is not int
        or type(lifetime) is not int
        or not 1 <= lifetime <= 365 * 86400
        or type(pending) is not bool
    ):
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
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_invitation_consents "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND recipient_context_hash=? "
            "AND purpose='review_invitation' AND granted_at<=? AND revoked_at IS NULL) THEN 1 ELSE 0 END"
        ).bind(store, environment, acquisition.acquisition_id, recipient_context_hash, now),
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
            0 if pending else now + lifetime,
        ),
        reviews_db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM acquisition_invitations "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND source_event_id=? "
            "AND recipient_context_hash=? AND subject_set_hash=? AND token_hash=? "
            "AND revoked_at IS NULL AND (expires_at>? OR (?=1 AND expires_at=0))) THEN 1 ELSE 0 END"
        ).bind(
            store,
            environment,
            acquisition.acquisition_id,
            event_id,
            recipient_context_hash,
            subject_hash,
            token_hash,
            now,
            int(pending),
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
            "AND i.token_hash=? AND s.subject_id=? AND i.revoked_at IS NULL "
            "AND (i.expires_at=-1 OR (i.expires_at>? AND i.expires_at>unixepoch()))"
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
        if record["payload_hash"] != payload_hash:
            raise ValueError("request conflict")
        if record["token_hash"] != token_hash:
            same_acquisition = _rows(
                await reviews_db.prepare(
                    "SELECT 1 ok FROM native_review_acquisitions WHERE store_key=? AND environment=? "
                    "AND review_id=? AND acquisition_id=? AND subject_id=?"
                )
                .bind(store, environment, record["review_id"], acquisition_id, subject)
                .all()
            )
            if len(same_acquisition) != 1:
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
            "AND i.revoked_at IS NULL AND (i.expires_at=-1 OR (i.expires_at>? AND i.expires_at>unixepoch())) "
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
                    "UPDATE acquisition_invitations SET revoked_at=COALESCE(revoked_at,?), "
                    "revocation_epoch=revocation_epoch+1 "
                    "WHERE store_key=? AND environment=? AND token_hash=?"
                ).bind(now, store, environment, token_hash),
                reviews_db.prepare(
                    "UPDATE review_recovery_requests SET state='failed' WHERE store_key=? AND environment=? "
                    "AND state='pending' AND request_id IN "
                    "(SELECT substr(g.request_key,10) FROM invitation_generations g "
                    "JOIN acquisition_invitations i USING(store_key,environment,acquisition_id) "
                    "WHERE i.store_key=? AND i.environment=? AND i.token_hash=? AND g.state='prepared' "
                    "AND g.request_key LIKE 'recovery-%')"
                ).bind(store, environment, store, environment, token_hash),
                reviews_db.prepare(
                    "UPDATE invitation_generations SET state='failed' WHERE store_key=? AND environment=? "
                    "AND state='prepared' AND acquisition_id IN (SELECT acquisition_id FROM acquisition_invitations "
                    "WHERE store_key=? AND environment=? AND token_hash=?)"
                ).bind(store, environment, store, environment, token_hash),
                reviews_db.prepare("DELETE FROM native_guard"),
            ]
        )
    except Exception as exc:
        raise ValueError("invitation unavailable") from exc


def _generation_token(secret, *, store, environment, acquisition, generation, recipient, key_version):
    material = json.dumps(
        ["reviews-invitation-generation-v1", store, environment, acquisition, generation, recipient, key_version],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


async def prepare_replacement(
    db,
    *,
    store,
    environment,
    acquisition,
    request_key,
    key_version,
    secret,
    now,
    random_token,
    recovery_request_id=None,
):
    """Prepare one replacement without activating it or recording a send.

    Only a trusted delivery adapter may call this. The destination is resolved
    from its previously verified recipient binding, never from a public request.
    """
    if (
        environment != "test"
        or not isinstance(secret, bytes)
        or len(secret) < 32
        or not isinstance(request_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_key)
        or (request_key.startswith("recovery-") and recovery_request_id is None)
        or (
            recovery_request_id is not None
            and (
                not isinstance(recovery_request_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", recovery_request_id)
                or request_key != "recovery-" + recovery_request_id
            )
        )
        or not isinstance(key_version, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_version)
        or type(now) is not int
    ):
        raise ValueError("invitation unavailable")
    if recovery_request_id is not None:
        pending = _rows(
            await db.prepare(
                "SELECT 1 ok FROM review_recovery_requests WHERE store_key=? AND environment=? "
                "AND acquisition_id=? AND request_id=? AND state='pending'"
            )
            .bind(store, environment, acquisition, recovery_request_id)
            .all()
        )
        if not pending:
            raise ValueError("invitation unavailable")
    anchors = _rows(
        await db.prepare(
            "SELECT i.recipient_context_hash,i.token_hash,i.revocation_epoch, "
            "CASE WHEN i.revoked_at IS NULL THEN 0 ELSE 1 END revoked "
            "FROM acquisition_invitations i "
            "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
            "WHERE i.store_key=? AND i.environment=? AND i.acquisition_id=? "
            "AND c.recipient_context_hash=i.recipient_context_hash AND c.purpose='review_invitation' "
            "AND c.granted_at<=? AND c.revoked_at IS NULL"
        )
        .bind(store, environment, acquisition, now)
        .all()
    )
    if len(anchors) != 1:
        raise ValueError("invitation unavailable")
    existing = _rows(
        await db.prepare(
            "SELECT generation_id,token_hash,key_version,delivery_identity,state FROM invitation_generations "
            "WHERE store_key=? AND environment=? AND request_key=? AND acquisition_id=?"
        )
        .bind(store, environment, request_key, acquisition)
        .all()
    )
    if existing:
        item = existing[0]
        token = _generation_token(
            secret,
            store=store,
            environment=environment,
            acquisition=acquisition,
            generation=item["generation_id"],
            recipient=anchors[0]["recipient_context_hash"],
            key_version=key_version,
        )
        if item["key_version"] != key_version or item["token_hash"] != hashlib.sha256(token.encode()).hexdigest():
            raise ValueError("invitation unavailable")
        result = {
            "generation_id": item["generation_id"],
            "delivery_identity": item["delivery_identity"],
            "state": item["state"],
        }
        if item["state"] == "prepared":
            result["token"] = token
        return result
    generation, delivery = random_token(), random_token()
    if not re.fullmatch(r"[0-9a-f]{64}", generation) or not re.fullmatch(r"[0-9a-f]{64}", delivery):
        raise ValueError("invitation unavailable")
    token = _generation_token(
        secret,
        store=store,
        environment=environment,
        acquisition=acquisition,
        generation=generation,
        recipient=anchors[0]["recipient_context_hash"],
        key_version=key_version,
    )
    try:
        await db.batch(
            [
                db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM acquisition_invitations i "
                    "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
                    "WHERE i.store_key=? AND i.environment=? AND i.acquisition_id=? AND i.token_hash=? "
                    "AND (CASE WHEN i.revoked_at IS NULL THEN 0 ELSE 1 END)=? "
                    "AND i.revocation_epoch=? "
                    "AND (?='' OR EXISTS(SELECT 1 FROM review_recovery_requests r "
                    "WHERE r.store_key=i.store_key AND r.environment=i.environment "
                    "AND r.acquisition_id=i.acquisition_id "
                    "AND r.request_id=? AND r.state='pending')) "
                    "AND c.recipient_context_hash=i.recipient_context_hash AND c.revoked_at IS NULL "
                    "AND c.purpose='review_invitation' AND c.granted_at<=?) THEN 1 ELSE 0 END"
                ).bind(
                    store,
                    environment,
                    acquisition,
                    anchors[0]["token_hash"],
                    anchors[0]["revoked"],
                    anchors[0]["revocation_epoch"],
                    recovery_request_id or "",
                    recovery_request_id or "",
                    now,
                ),
                db.prepare(
                    "INSERT INTO invitation_generations "
                    "(store_key,environment,acquisition_id,generation_id,request_key,token_hash,key_version,"
                    "expected_token_hash,expected_revoked,expected_revocation_epoch,"
                    "delivery_identity,state,prepared_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'prepared',?)"
                ).bind(
                    store,
                    environment,
                    acquisition,
                    generation,
                    request_key,
                    hashlib.sha256(token.encode()).hexdigest(),
                    key_version,
                    anchors[0]["token_hash"],
                    anchors[0]["revoked"],
                    anchors[0]["revocation_epoch"],
                    delivery,
                    now,
                ),
                db.prepare("DELETE FROM native_guard"),
            ]
        )
    except Exception as exc:
        concurrent = _rows(
            await db.prepare(
                "SELECT generation_id FROM invitation_generations WHERE store_key=? AND environment=? "
                "AND acquisition_id=? AND request_key=?"
            )
            .bind(store, environment, acquisition, request_key)
            .all()
        )
        if concurrent:
            return await prepare_replacement(
                db,
                store=store,
                environment=environment,
                acquisition=acquisition,
                request_key=request_key,
                key_version=key_version,
                secret=secret,
                now=now,
                random_token=random_token,
                recovery_request_id=recovery_request_id,
            )
        raise ValueError("invitation unavailable") from exc
    return {"token": token, "generation_id": generation, "delivery_identity": delivery, "state": "prepared"}


async def claim_delivery(db, *, store, environment, acquisition, generation, delivery, now):
    """Claim a prepared delivery once; an unknown result must be reconciled."""
    if (
        environment != "test"
        or type(now) is not int
        or not isinstance(generation, str)
        or not re.fullmatch(r"[0-9a-f]{64}", generation)
        or not isinstance(delivery, str)
        or not re.fullmatch(r"[0-9a-f]{64}", delivery)
    ):
        raise ValueError("delivery unavailable")
    try:
        await db.batch(
            [
                db.prepare(
                    "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM invitation_generations g "
                    "JOIN acquisition_invitations i USING(store_key,environment,acquisition_id) "
                    "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
                    "WHERE g.store_key=? AND g.environment=? AND g.acquisition_id=? AND g.generation_id=? "
                    "AND g.delivery_identity=? AND g.state='prepared' AND i.token_hash=g.expected_token_hash "
                    "AND i.revocation_epoch=g.expected_revocation_epoch AND c.revoked_at IS NULL "
                    "AND c.purpose='review_invitation' AND c.granted_at<=? "
                    "AND (g.request_key NOT LIKE 'recovery-%' OR EXISTS(SELECT 1 FROM review_recovery_requests r "
                    "WHERE r.store_key=g.store_key AND r.environment=g.environment "
                    "AND r.acquisition_id=g.acquisition_id "
                    "AND r.request_id=substr(g.request_key,10) AND r.state='pending')) "
                    "AND c.recipient_context_hash=i.recipient_context_hash "
                    "AND NOT EXISTS(SELECT 1 FROM review_recipient_suppressions rs WHERE "
                    "rs.store_key=i.store_key AND rs.environment=i.environment "
                    "AND rs.recipient_context_hash=i.recipient_context_hash AND rs.purpose='review_invitation')"
                    ") THEN 1 ELSE 0 END"
                ).bind(store, environment, acquisition, generation, delivery, now),
                db.prepare(
                    "UPDATE invitation_generations SET state='dispatching' WHERE store_key=? AND environment=? "
                    "AND acquisition_id=? AND generation_id=? AND state='prepared'"
                ).bind(store, environment, acquisition, generation),
                db.prepare("DELETE FROM native_guard"),
            ]
        )
    except Exception as exc:
        raise ValueError("delivery unavailable") from exc
    return {"state": "dispatching"}


async def record_delivery_outcome(
    db, *, store, environment, acquisition, generation, delivery, outcome, now, accepted_at=None, lifetime=60 * 86400
):
    """Record a trusted provider outcome; only acceptance activates a generation."""
    if (
        environment != "test"
        or outcome not in ("accepted", "failed", "uncertain")
        or not isinstance(generation, str)
        or not re.fullmatch(r"[0-9a-f]{64}", generation)
        or not isinstance(delivery, str)
        or not re.fullmatch(r"[0-9a-f]{64}", delivery)
        or type(now) is not int
        or (outcome == "accepted" and (type(accepted_at) is not int or accepted_at > now))
        or (outcome != "accepted" and accepted_at is not None)
        or (lifetime is not None and (type(lifetime) is not int or not 1 <= lifetime <= 365 * 86400))
    ):
        raise ValueError("delivery unavailable")
    current = _rows(
        await db.prepare(
            "SELECT token_hash,expected_token_hash,expected_revoked,state,prepared_at,accepted_at, "
            "key_version,request_key "
            "FROM invitation_generations "
            "WHERE store_key=? "
            "AND environment=? AND acquisition_id=? AND generation_id=? AND delivery_identity=?"
        )
        .bind(store, environment, acquisition, generation, delivery)
        .all()
    )
    if len(current) != 1:
        raise ValueError("delivery unavailable")
    item = current[0]
    if item["state"] == "revoked" and outcome == "accepted" and item["accepted_at"] == accepted_at:
        return {"state": "revoked"}
    if item["state"] == outcome:
        if outcome == "accepted" and item["accepted_at"] != accepted_at:
            raise ValueError("delivery unavailable")
        return {"state": outcome}
    if outcome == "accepted" and accepted_at < item["prepared_at"]:
        raise ValueError("delivery unavailable")
    if item["state"] not in ("prepared", "dispatching", "uncertain") or (
        item["state"] == "prepared" and outcome != "failed"
    ):
        raise ValueError("delivery unavailable")
    if outcome == "accepted":
        guard = db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM invitation_generations g "
            "JOIN acquisition_invitations i USING(store_key,environment,acquisition_id) "
            "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
            "WHERE g.store_key=? AND g.environment=? AND g.acquisition_id=? AND g.generation_id=? "
            "AND g.delivery_identity=? AND g.state=? "
            "AND i.token_hash=g.expected_token_hash "
            "AND (CASE WHEN i.revoked_at IS NULL THEN 0 ELSE 1 END)=g.expected_revoked "
            "AND i.revocation_epoch=g.expected_revocation_epoch "
            "AND (g.request_key NOT LIKE 'recovery-%' OR EXISTS(SELECT 1 FROM review_recovery_requests r "
            "WHERE r.store_key=g.store_key AND r.environment=g.environment AND r.acquisition_id=g.acquisition_id "
            "AND r.request_id=substr(g.request_key,10) AND r.state='pending')) "
            "AND c.recipient_context_hash=i.recipient_context_hash AND c.revoked_at IS NULL "
            "AND c.purpose='review_invitation' AND c.granted_at<=? "
            "AND NOT EXISTS(SELECT 1 FROM review_recipient_suppressions rs WHERE "
            "rs.store_key=i.store_key AND rs.environment=i.environment "
            "AND rs.recipient_context_hash=i.recipient_context_hash AND rs.purpose='review_invitation')"
            ") THEN 1 ELSE 0 END"
        ).bind(store, environment, acquisition, generation, delivery, item["state"], now)
    else:
        # Recording a failure or an uncertain outcome must remain possible
        # even if consent or the previous invitation was revoked meanwhile.
        guard = db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM invitation_generations "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND generation_id=? "
            "AND delivery_identity=? AND state=?) THEN 1 ELSE 0 END"
        ).bind(store, environment, acquisition, generation, delivery, item["state"])
    statements = [guard]
    if outcome == "accepted":
        expiry = -1 if lifetime is None else accepted_at + lifetime
        statements.extend(
            [
                db.prepare(
                    "UPDATE invitation_generations SET state='revoked' WHERE store_key=? AND environment=? "
                    "AND acquisition_id=? AND token_hash=? AND state='accepted'"
                ).bind(store, environment, acquisition, item["expected_token_hash"]),
                db.prepare(
                    "UPDATE acquisition_invitations SET token_hash=?,expires_at=?,revoked_at=NULL,active_key_version=? "
                    "WHERE store_key=? AND environment=? AND acquisition_id=? AND token_hash=?"
                ).bind(
                    item["token_hash"],
                    expiry,
                    item["key_version"],
                    store,
                    environment,
                    acquisition,
                    item["expected_token_hash"],
                ),
                db.prepare(
                    "UPDATE invitation_generations SET state='accepted',accepted_at=?,expires_at=? "
                    "WHERE store_key=? AND environment=? AND acquisition_id=? AND generation_id=?"
                ).bind(accepted_at, expiry, store, environment, acquisition, generation),
            ]
        )
        if item["request_key"].startswith("recovery-"):
            statements.append(
                db.prepare(
                    "UPDATE review_recovery_requests SET state='fulfilled' WHERE store_key=? "
                    "AND environment=? AND acquisition_id=? AND request_id=? AND state='pending'"
                ).bind(store, environment, acquisition, item["request_key"][9:])
            )
    else:
        statements.append(
            db.prepare(
                "UPDATE invitation_generations SET state=? WHERE store_key=? AND environment=? AND acquisition_id=? "
                "AND generation_id=?"
            ).bind(outcome, store, environment, acquisition, generation)
        )
        if outcome == "failed" and item["request_key"].startswith("recovery-"):
            statements.append(
                db.prepare(
                    "UPDATE review_recovery_requests SET state='failed' WHERE store_key=? AND environment=? "
                    "AND acquisition_id=? AND request_id=? AND state='pending'"
                ).bind(store, environment, acquisition, item["request_key"][9:])
            )
    statements.append(db.prepare("DELETE FROM native_guard"))
    try:
        await db.batch(statements)
    except Exception as exc:
        latest = _rows(
            await db.prepare(
                "SELECT state,accepted_at FROM invitation_generations WHERE store_key=? AND environment=? "
                "AND acquisition_id=? AND generation_id=? AND delivery_identity=?"
            )
            .bind(store, environment, acquisition, generation, delivery)
            .all()
        )
        if latest:
            state = latest[0]["state"]
            if state == outcome and (outcome != "accepted" or latest[0]["accepted_at"] == accepted_at):
                return {"state": state}
            if state == "revoked" and outcome == "accepted" and latest[0]["accepted_at"] == accepted_at:
                return {"state": "revoked"}
        if outcome == "accepted":
            try:
                await db.batch(
                    [
                        db.prepare(
                            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM invitation_generations g "
                            "JOIN acquisition_invitations i USING(store_key,environment,acquisition_id) "
                            "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
                            "WHERE g.store_key=? AND g.environment=? AND g.acquisition_id=? AND g.generation_id=? "
                            "AND g.delivery_identity=? AND g.state=? AND "
                            "(i.token_hash<>g.expected_token_hash OR i.revocation_epoch<>g.expected_revocation_epoch "
                            "OR c.recipient_context_hash<>i.recipient_context_hash OR c.revoked_at IS NOT NULL "
                            "OR (g.request_key LIKE 'recovery-%' AND NOT EXISTS("
                            "SELECT 1 FROM review_recovery_requests r "
                            "WHERE r.store_key=g.store_key AND r.environment=g.environment "
                            "AND r.acquisition_id=g.acquisition_id AND r.request_id=substr(g.request_key,10) "
                            "AND r.state='pending')))) "
                            "THEN 1 ELSE 0 END"
                        ).bind(store, environment, acquisition, generation, delivery, item["state"]),
                        db.prepare(
                            "UPDATE invitation_generations SET state='revoked',accepted_at=?,expires_at=? "
                            "WHERE store_key=? AND environment=? AND acquisition_id=? AND generation_id=? "
                            "AND state IN ('dispatching','uncertain')"
                        ).bind(accepted_at, expiry, store, environment, acquisition, generation),
                        db.prepare(
                            "UPDATE review_recovery_requests SET state='failed' WHERE store_key=? AND environment=? "
                            "AND acquisition_id=? AND request_id=? AND state='pending'"
                        ).bind(
                            store,
                            environment,
                            acquisition,
                            item["request_key"][9:] if item["request_key"].startswith("recovery-") else "",
                        ),
                        db.prepare("DELETE FROM native_guard"),
                    ]
                )
                return {"state": "revoked"}
            except Exception:
                pass
        raise ValueError("delivery unavailable") from exc
    return {"state": outcome}


async def prepare_initial_delivery(
    reviews_db,
    commerce_db,
    *,
    store,
    environment,
    event_id,
    subject_maps,
    recipient_context_hash,
    request_key,
    key_version,
    secret,
    now,
    random_token,
):
    """Prepare an inactive initial generation for a confirmed, consented acquisition."""
    acquisition = await confirmed_purchase(
        commerce_db, store=store, environment=environment, event_id=event_id, subject_maps=subject_maps
    )
    existing = _rows(
        await reviews_db.prepare(
            "SELECT recipient_context_hash,subject_set_hash FROM acquisition_invitations "
            "WHERE store_key=? AND environment=? AND acquisition_id=?"
        )
        .bind(store, environment, acquisition.acquisition_id)
        .all()
    )
    if existing:
        if existing[0]["recipient_context_hash"] != recipient_context_hash or existing[0][
            "subject_set_hash"
        ] != _digest(acquisition.subject_ids):
            raise ValueError("invitation unavailable")
    else:
        await issue_from_settlement(
            reviews_db,
            commerce_db,
            store=store,
            environment=environment,
            event_id=event_id,
            subject_maps=subject_maps,
            recipient_context_hash=recipient_context_hash,
            token_key=secret,
            now=now,
            pending=True,
        )
    generation = await prepare_replacement(
        reviews_db,
        store=store,
        environment=environment,
        acquisition=acquisition.acquisition_id,
        request_key=request_key,
        key_version=key_version,
        secret=secret,
        now=now,
        random_token=random_token,
    )
    return generation | {"acquisition_id": acquisition.acquisition_id, "subjects": acquisition.subject_ids}
