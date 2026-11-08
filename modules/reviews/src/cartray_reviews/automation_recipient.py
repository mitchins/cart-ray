"""Synthetic recipient binding and purpose-bound consent checks for held intents."""

import hashlib
import hmac
import json
import re

HEX = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def record_recipient(db, *, store, environment, data, signature, secret, now):
    """Accept an independently signed test binding for an existing held intent only."""
    keys = {"outbox_id", "recipient_context_hash", "binding_version", "evidence_reference", "verified_at"}
    if not isinstance(data, dict) or set(data) != keys or environment != "test":
        raise ValueError("recipient evidence unavailable")
    outbox, recipient, version = data["outbox_id"], data["recipient_context_hash"], data["binding_version"]
    reference, verified = data["evidence_reference"], data["verified_at"]
    if (
        not isinstance(store, str)
        or not ID.fullmatch(store)
        or type(outbox) is not int
        or outbox < 1
        or not isinstance(recipient, str)
        or not HEX.fullmatch(recipient)
        or not isinstance(version, str)
        or not ID.fullmatch(version)
        or not isinstance(reference, str)
        or not REFERENCE.fullmatch(reference)
        or type(verified) is not int
        or type(now) is not int
        or not 0 < verified <= now
        or not isinstance(secret, bytes)
        or len(secret) < 32
        or not isinstance(signature, str)
        or not HEX.fullmatch(signature)
    ):
        raise ValueError("recipient evidence unavailable")
    signed = json.dumps(
        ["reviews-automation-recipient-v1", store, environment, data],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if not hmac.compare_digest(hmac.new(secret, signed, hashlib.sha256).hexdigest(), signature):
        raise ValueError("recipient evidence unavailable")
    # An already authenticated, byte-identical old binding remains a valid
    # replay after rotation. Do not attempt a new insert through the key fence.
    existing = _rows(
        await db.prepare(
            "SELECT outbox_id FROM review_automation_recipient_bindings WHERE store_key=? AND environment=? "
            "AND outbox_id=? AND recipient_context_hash=? AND binding_version=? "
            "AND evidence_reference=? AND verified_at=?"
        ).bind(store, environment, outbox, recipient, version, reference, verified).all()
    )
    if len(existing) == 1:
        return
    try:
        await db.batch(
            [
                db.prepare(
                    "INSERT OR IGNORE INTO review_automation_recipient_bindings "
                    "SELECT store_key,environment,acquisition_id,outbox_id,?,?,?,? FROM review_automation_intents "
                    "WHERE store_key=? AND environment=? AND outbox_id=? "
                    "AND state='held_eligibility_recipient_consent'"
                ).bind(recipient, version, reference, verified, store, environment, outbox),
            ]
        )
    except Exception as exc:
        raise ValueError("recipient evidence conflict") from exc
    bound = _rows(
        await db.prepare(
            "SELECT outbox_id FROM review_automation_recipient_bindings WHERE store_key=? AND environment=? "
            "AND outbox_id=? AND recipient_context_hash=? AND binding_version=? "
            "AND evidence_reference=? AND verified_at=?"
        )
        .bind(store, environment, outbox, recipient, version, reference, verified)
        .all()
    )
    if len(bound) != 1:
        raise ValueError("recipient evidence unavailable")


async def evidence_status(db, *, store, environment, outbox_id, now):
    """Report matching evidence only; no delivery or eligibility approval is implied."""
    if (
        not isinstance(store, str)
        or not ID.fullmatch(store)
        or environment != "test"
        or type(outbox_id) is not int
        or outbox_id < 1
        or type(now) is not int
        or now < 1
    ):
        raise ValueError("evidence unavailable")
    result = _rows(
        await db.prepare(
            "SELECT i.state, CASE WHEN b.recipient_context_hash IS NOT NULL AND b.verified_at<=? THEN 1 ELSE 0 END "
            "recipient_verified, CASE WHEN b.recipient_context_hash IS NOT NULL AND b.verified_at<=? AND EXISTS("
            "SELECT 1 FROM review_invitation_consents c WHERE c.store_key=i.store_key AND c.environment=i.environment "
            "AND c.acquisition_id=i.acquisition_id AND c.recipient_context_hash=b.recipient_context_hash "
            "AND c.purpose='review_invitation' AND c.granted_at<=? AND c.revoked_at IS NULL) "
            "THEN 1 ELSE 0 END consent_granted FROM review_automation_intents i "
            "LEFT JOIN review_automation_recipient_bindings b ON b.store_key=i.store_key "
            "AND b.environment=i.environment AND b.acquisition_id=i.acquisition_id AND b.outbox_id=i.outbox_id "
            "WHERE i.store_key=? AND i.environment=? AND i.outbox_id=? LIMIT 1"
        )
        .bind(now, now, now, store, environment, outbox_id)
        .all()
    )
    if len(result) != 1:
        raise ValueError("evidence unavailable")
    return {
        "state": result[0]["state"],
        "recipient_binding_recorded": bool(result[0]["recipient_verified"]),
        "consent_granted": bool(result[0]["consent_granted"]),
    }


async def resolve_replay(db, *, store, environment, outbox_id, candidate):
    """Resolve an old immutable binding only when its new-key alias matches."""
    if (not isinstance(store, str) or not ID.fullmatch(store) or environment != "test"
            or type(outbox_id) is not int or outbox_id < 1 or not isinstance(candidate, str)
            or not HEX.fullmatch(candidate)):
        raise ValueError("evidence unavailable")
    records = _rows(await db.prepare(
        "SELECT b.recipient_context_hash,b.binding_version,b.verified_at,"
        "c.evidence_reference,c.granted_at,c.revoked_at,"
        "EXISTS(SELECT 1 FROM review_recipient_suppressions s WHERE s.store_key=b.store_key "
        "AND s.environment=b.environment AND s.recipient_context_hash=b.recipient_context_hash "
        "AND s.purpose='review_invitation') AS suppressed "
        "FROM review_automation_recipient_bindings b LEFT JOIN review_invitation_consents c "
        "ON c.store_key=b.store_key AND c.environment=b.environment AND c.acquisition_id=b.acquisition_id "
        "AND c.recipient_context_hash=b.recipient_context_hash AND c.purpose='review_invitation' "
        "WHERE b.store_key=? AND b.environment=? AND b.outbox_id=?"
    ).bind(store, environment, outbox_id).all())
    if not records:
        return {"status": "new"}
    item = records[0]
    if item["recipient_context_hash"] != candidate:
        match = _rows(await db.prepare(
            "SELECT 1 ok FROM review_suppression_aliases a JOIN review_suppression_aliases b "
            "USING(store_key,environment,canonical_hash) WHERE a.store_key=? AND a.environment=? "
            "AND a.recipient_context_hash=? AND b.recipient_context_hash=? LIMIT 1"
        ).bind(store, environment, item["recipient_context_hash"], candidate).all())
        if not match:
            raise ValueError("evidence unavailable")
    if item["suppressed"] or item["revoked_at"] is not None:
        return {"status": "suppressed"}
    return {"status": "existing", "recipient_context_hash": item["recipient_context_hash"],
            "binding_version": item["binding_version"], "verified_at": item["verified_at"],
            "consent_reference": item["evidence_reference"], "granted_at": item["granted_at"]}


async def authorize_historical_recipient(db, *, store, environment, data, now, random_token):
    """Match original delivery and recipient without granting present sending authority."""
    if (environment != "test" or not isinstance(data, dict)
            or set(data) != {"outbox_id", "acquisition_id", "generation_id", "delivery_identity", "candidate"}
            or type(data["outbox_id"]) is not int or data["outbox_id"] < 1
            or not isinstance(data["acquisition_id"], str) or not ID.fullmatch(data["acquisition_id"])
            or any(not isinstance(data[key], str) or not HEX.fullmatch(data[key])
                   for key in ("generation_id", "delivery_identity", "candidate"))):
        raise ValueError("evidence unavailable")
    found = _rows(await db.prepare(
        "SELECT b.recipient_context_hash FROM invitation_generations g "
        "JOIN review_automation_intents i USING(store_key,environment,acquisition_id) "
        "JOIN review_automation_recipient_bindings b USING(store_key,environment,acquisition_id,outbox_id) "
        "WHERE g.store_key=? AND g.environment=? AND i.outbox_id=? AND g.acquisition_id=? "
        "AND g.generation_id=? AND g.delivery_identity=? "
        "AND g.state IN ('dispatching','uncertain','accepted','revoked') "
        "AND (g.request_key=('initial_' || i.outbox_id) OR g.request_key LIKE 'recovery-%') LIMIT 1"
    ).bind(store, environment, data["outbox_id"], data["acquisition_id"], data["generation_id"],
           data["delivery_identity"]).all())
    if len(found) != 1:
        raise ValueError("evidence unavailable")
    original = found[0]["recipient_context_hash"]
    if original != data["candidate"]:
        aliases = _rows(await db.prepare(
            "SELECT 1 ok FROM review_suppression_aliases a JOIN review_suppression_aliases b "
            "USING(store_key,environment,canonical_hash) WHERE a.store_key=? AND a.environment=? "
            "AND a.recipient_context_hash=? AND b.recipient_context_hash=? LIMIT 1"
        ).bind(store, environment, original, data["candidate"]).all())
        if not aliases:
            raise ValueError("evidence unavailable")
    await db.batch([db.prepare("INSERT INTO review_historical_recipient_audit VALUES (?,?,?,?,?,?,?)").bind(
        random_token(), store, environment, data["acquisition_id"], data["generation_id"],
        data["delivery_identity"], now)])
    return {"status": "matched"}
