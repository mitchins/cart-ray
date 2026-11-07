"""Test-only due-intent authorization and merchant-scoped suppression."""

import hashlib
import hmac
import json
import re


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def due_initial(db, *, store, environment, outbox_id, now):
    """Return the stored recipient only when due and its permission remains live."""
    if environment != "test" or type(outbox_id) is not int or outbox_id < 1 or type(now) is not int:
        raise ValueError("initial invitation unavailable")
    found = _rows(
        await db.prepare(
            "SELECT i.acquisition_id,b.recipient_context_hash FROM review_automation_intents i "
            "JOIN review_automation_intake t USING(store_key,environment,outbox_id) "
            "JOIN review_automation_recipient_bindings b USING(store_key,environment,acquisition_id,outbox_id) "
            "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id,recipient_context_hash) "
            "WHERE i.store_key=? AND i.environment=? AND i.outbox_id=? AND i.due_at<=? "
            "AND t.disposition='accepted' AND b.verified_at<=? AND c.purpose='review_invitation' "
            "AND c.granted_at<=? AND c.revoked_at IS NULL AND b.binding_version='v2' "
            "AND NOT EXISTS(SELECT 1 FROM review_recipient_suppressions r WHERE "
            "r.store_key=i.store_key AND r.environment=i.environment "
            "AND r.recipient_context_hash=b.recipient_context_hash AND r.purpose='review_invitation') "
            "AND NOT EXISTS(SELECT 1 FROM review_invitation_consent_withdrawals w WHERE "
            "w.store_key=i.store_key AND w.environment=i.environment AND w.acquisition_id=i.acquisition_id "
            "AND w.recipient_context_hash=b.recipient_context_hash AND w.purpose='review_invitation') LIMIT 1"
        )
        .bind(store, environment, outbox_id, now, now, now)
        .all()
    )
    if len(found) != 1:
        raise ValueError("initial invitation unavailable")
    return found[0]


def unsubscribe_token(*, secret, store, environment, outbox_id):
    if len(secret) < 32 or environment != "test" or type(outbox_id) is not int or outbox_id < 1:
        raise ValueError("unsubscribe unavailable")
    value = json.dumps(["review-unsubscribe-v1", store, environment, outbox_id], separators=(",", ":")).encode()
    return str(outbox_id) + "." + hmac.new(secret, value, hashlib.sha256).hexdigest()


async def unsubscribe(db, *, store, environment, token, secret, now):
    """Atomically record withdrawal and suppress future queued deliveries."""
    if not isinstance(token, str) or not re.fullmatch(r"[1-9][0-9]{0,15}\.[a-f0-9]{64}", token):
        raise ValueError("unsubscribe unavailable")
    outbox_id = int(token.split(".")[0])
    if not hmac.compare_digest(
        token, unsubscribe_token(secret=secret, store=store, environment=environment, outbox_id=outbox_id)
    ):
        raise ValueError("unsubscribe unavailable")
    found = _rows(
        await db.prepare(
            "SELECT acquisition_id,recipient_context_hash FROM review_automation_recipient_bindings "
            "WHERE store_key=? AND environment=? AND outbox_id=? AND binding_version='v2'"
        )
        .bind(store, environment, outbox_id)
        .all()
    )
    if len(found) != 1 or type(now) is not int or now < 1:
        raise ValueError("unsubscribe unavailable")
    acquisition, recipient = found[0]["acquisition_id"], found[0]["recipient_context_hash"]
    await db.batch(
        [
            db.prepare(
                "INSERT OR IGNORE INTO review_recipient_suppressions "
                "(store_key,environment,recipient_context_hash,purpose,created_at) "
                "VALUES (?,?,?,'review_invitation',?)"
            ).bind(store, environment, recipient, now),
            db.prepare(
                "INSERT INTO review_invitation_consent_withdrawals "
                "SELECT ?,?,?,?,'review_invitation',?,? WHERE NOT EXISTS(SELECT 1 "
                "FROM review_invitation_consent_withdrawals WHERE store_key=? AND environment=? "
                "AND acquisition_id=? AND recipient_context_hash=? AND purpose='review_invitation')"
            ).bind(
                store,
                environment,
                acquisition,
                recipient,
                "unsubscribe_" + str(outbox_id),
                now,
                store,
                environment,
                acquisition,
                recipient,
            ),
            db.prepare(
                "UPDATE review_invitation_consents SET revoked_at=COALESCE(revoked_at,?) "
                "WHERE store_key=? AND environment=? AND acquisition_id=? "
                "AND recipient_context_hash=? AND purpose='review_invitation'"
            ).bind(now, store, environment, acquisition, recipient),
        ]
    )
