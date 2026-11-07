"""Test-only, keyed due-intent evaluation claims; never delivery authorization.

Callers must authenticate and supply a trusted clock and unpredictable token. No public
route or timer is wired: the eventual queue adapter must enforce those boundaries.
"""

import re

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOKEN = re.compile(r"[a-f0-9]{64}\Z")
_MAX_TIME = 253402300799


def _validate(store, environment, acquisition, token, now):
    if (
        not isinstance(store, str)
        or not _ID.fullmatch(store)
        or environment != "test"
        or not isinstance(acquisition, str)
        or not _ID.fullmatch(acquisition)
        or not isinstance(token, str)
        or not _TOKEN.fullmatch(token)
        or type(now) is not int
        or not 0 < now <= _MAX_TIME - 300
    ):
        raise ValueError("claim unavailable")


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def claim_due(db, *, store, environment, acquisition, token, now, lease_seconds):
    """Atomically claim one explicitly addressed due intent, or return no claim.

    A repeated token during its lease returns the same generation. Expired claims can
    be taken over with a new generation. Completed evaluations never re-open.
    """
    _validate(store, environment, acquisition, token, now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise ValueError("claim unavailable")
    result = (
        await db.prepare(
            "INSERT INTO review_automation_due_claims "
            "SELECT store_key,environment,acquisition_id,?,1,?,'claimed' FROM review_automation_intents "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND due_at<=? "
            "AND state='held_eligibility_recipient_consent' "
            "ON CONFLICT(store_key,environment,acquisition_id) DO UPDATE SET "
            "claim_token=excluded.claim_token, generation=review_automation_due_claims.generation+1, "
            "lease_until=excluded.lease_until WHERE review_automation_due_claims.state='claimed' "
            "AND review_automation_due_claims.lease_until<=? RETURNING generation,lease_until"
        )
        .bind(token, now + lease_seconds, store, environment, acquisition, now, now)
        .all()
    )
    rows = _rows(result)
    if rows:
        return rows[0]
    rows = _rows(
        await db.prepare(
            "SELECT generation,lease_until FROM review_automation_due_claims "
            "WHERE store_key=? AND environment=? AND acquisition_id=? AND claim_token=? "
            "AND state='claimed' AND lease_until>?"
        )
        .bind(store, environment, acquisition, token, now)
        .all()
    )
    return rows[0] if rows else None


async def complete_claim(db, *, store, environment, acquisition, token, generation, now):
    """Fence stale workers; completion records evaluation only, never delivery."""
    _validate(store, environment, acquisition, token, now)
    if type(generation) is not int or generation < 1:
        raise ValueError("claim unavailable")
    rows = _rows(
        await db.prepare(
            "UPDATE review_automation_due_claims SET state='completed' WHERE store_key=? AND environment=? "
            "AND acquisition_id=? AND claim_token=? AND generation=? AND state='claimed' AND lease_until>? "
            "RETURNING generation"
        )
        .bind(store, environment, acquisition, token, generation, now)
        .all()
    )
    return bool(rows)
