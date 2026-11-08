"""Accept a private recovery command after its durable queue delivery."""

import re


def rows(result):
    """Convert a D1 result without exposing database metadata."""
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def accept_recovery_command(db, *, store, environment, token_hash, request_id, issued_at, now):
    """Select a consent-bound acquisition and its canonical pending request."""
    if environment != "test" or not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in (token_hash, request_id)
    ):
        raise ValueError("invalid recovery command")
    if type(issued_at) is not int or issued_at > now:
        raise ValueError("invalid recovery command")
    found = rows(
        await db.prepare(
            "SELECT i.acquisition_id FROM acquisition_invitations i "
            "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
            "WHERE i.store_key=? AND i.environment=? AND c.purpose='review_invitation' "
            "AND c.recipient_context_hash=i.recipient_context_hash AND c.revoked_at IS NULL AND "
            "((i.token_hash=? AND (i.expires_at>0 OR i.expires_at=-1)) "
            "OR EXISTS(SELECT 1 FROM invitation_generations g "
            "WHERE g.store_key=i.store_key AND g.environment=i.environment "
            "AND g.acquisition_id=i.acquisition_id AND g.token_hash=? AND g.accepted_at IS NOT NULL)) LIMIT 1"
        )
        .bind(store, environment, token_hash, token_hash)
        .all()
    )
    if not found:
        return None
    acquisition = found[0]["acquisition_id"]
    # Stale messages may read an existing pending request but never write: a
    # concurrent fulfilment must not rearm a request after the cooldown.
    stale = now - issued_at >= 86400
    if not stale:
        await db.batch(
            [
                db.prepare(
                    "INSERT INTO review_recovery_requests VALUES (?,?,?,?,?,'pending') "
                    "ON CONFLICT(store_key,environment,acquisition_id) DO UPDATE SET "
                    "request_id=excluded.request_id,requested_at=excluded.requested_at,state='pending' "
                    "WHERE review_recovery_requests.state IN ('fulfilled','failed') "
                    "AND review_recovery_requests.request_id<>excluded.request_id "
                    "AND review_recovery_requests.requested_at<=excluded.requested_at-86400"
                ).bind(store, environment, acquisition, request_id, now)
            ]
        )
    pending = rows(
        await db.prepare(
            "SELECT r.acquisition_id,r.request_id,i.source_event_id,i.recipient_context_hash,a.outbox_id "
            "FROM review_recovery_requests r JOIN acquisition_invitations i "
            "USING(store_key,environment,acquisition_id) JOIN review_invitation_consents c "
            "USING(store_key,environment,acquisition_id) "
            "LEFT JOIN review_automation_intents a USING(store_key,environment,acquisition_id) "
            "WHERE r.store_key=? AND r.environment=? AND r.acquisition_id=? AND r.state='pending' "
            "AND c.revoked_at IS NULL AND c.purpose='review_invitation' "
            "AND c.recipient_context_hash=i.recipient_context_hash LIMIT 1"
        )
        .bind(store, environment, acquisition)
        .all()
    )
    if stale and not pending:
        raise ValueError("stale recovery command")
    return pending[0] if pending else None
