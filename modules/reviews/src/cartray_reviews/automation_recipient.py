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
