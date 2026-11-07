"""Fenced, test-only key rotation using verified old/new contact pseudonyms.

No address is written to D1. Operators must pass contacts obtained from the
verified historical purchase source, pause old producer deployments and keep the
fence closed across the independent context-D1 key switch.
"""

import hashlib
import hmac
import json
import re

_ID = re.compile(r"[A-Za-z0-9_-]{1,60}\Z")
_SCOPE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


def _validate(store, environment, old, target):
    if not _SCOPE.fullmatch(store or "") or environment != "test" or not _ID.fullmatch(old or ""):
        raise ValueError("rotation unavailable")
    if not _ID.fullmatch(target or "") or old == target:
        raise ValueError("rotation unavailable")


def _digest(key, store, email):
    if not isinstance(key, str) or len(key) < 32 or not isinstance(email, str):
        raise ValueError("rotation unavailable")
    if email != email.strip() or len(email) > 254 or not _EMAIL.fullmatch(email):
        raise ValueError("rotation unavailable")
    local, domain = email.rsplit("@", 1)
    message = json.dumps(["acorn-recipient-v2", store, "test", local + "@" + domain.lower()],
                         separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def _coverage_sql():
    return (
        "EXISTS(SELECT 1 FROM review_suppression_rotation "
        "WHERE store_key=? AND environment=? AND state='frozen' AND active_binding_version=? "
        "AND target_binding_version=? AND old_key_check IS NOT NULL AND target_key_check IS NOT NULL) "
        "AND NOT EXISTS(SELECT 1 FROM review_automation_recipient_bindings b "
        "WHERE b.store_key=? AND b.environment=? AND b.binding_version='v1') "
        "AND NOT EXISTS(SELECT 1 FROM review_automation_recipient_bindings b "
        "WHERE b.store_key=? AND b.environment=? AND (b.binding_version='v2' OR b.binding_version GLOB 'v2_*') "
        "AND NOT EXISTS(SELECT 1 FROM review_suppression_aliases a JOIN review_suppression_aliases n "
        "USING(store_key,environment,canonical_hash) WHERE a.store_key=b.store_key "
        "AND a.environment=b.environment AND a.recipient_context_hash=b.recipient_context_hash "
        "AND n.binding_version=?)) "
        "AND NOT EXISTS(SELECT 1 FROM review_recipient_suppressions s "
        "WHERE s.store_key=? AND s.environment=? AND NOT EXISTS(SELECT 1 FROM review_suppression_aliases a "
        "JOIN review_suppression_aliases n USING(store_key,environment,canonical_hash) "
        "WHERE a.store_key=s.store_key AND a.environment=s.environment "
        "AND a.recipient_context_hash=s.recipient_context_hash AND n.binding_version=?))"
    )


def _coverage_params(store, environment, old, target):
    return (store, environment, "v2_" + old, "v2_" + target, store, environment,
            store, environment, "v2_" + target, store, environment, "v2_" + target)


def _key_check(key, store, environment, installation, version):
    return hmac.new(key.encode(), json.dumps(
        ["reviews-suppression-key-check-v1", store, environment, installation, version],
        separators=(",", ":"), ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def _rotation_key_check(key, store, environment, version):
    return hmac.new(key.encode(), json.dumps(
        ["reviews-rotation-key-proof-v1", store, environment, version],
        separators=(",", ":"), ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


async def freeze(db, *, store, environment, old, target):
    """Atomically fence inserts and claims; unresolved provider calls block it."""
    _validate(store, environment, old, target)
    old, target = "v2_" + old, "v2_" + target
    try:
        await db.batch([
            db.prepare(
                "INSERT INTO native_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM invitation_generations "
                "WHERE store_key=? AND environment=? AND state IN ('dispatching','uncertain')) "
                "AND NOT EXISTS(SELECT 1 FROM review_suppression_rotation WHERE store_key=? AND environment=? "
                "AND NOT (state='active' AND active_binding_version=? OR "
                "state='frozen' AND active_binding_version=? AND target_binding_version=?)) "
                "THEN 1 ELSE 0 END"
            ).bind(store, environment, store, environment, old, old, target),
            db.prepare(
                "INSERT INTO review_suppression_rotation "
                "(store_key,environment,state,active_binding_version,target_binding_version) VALUES (?,?,'frozen',?,?) "
                "ON CONFLICT(store_key,environment) DO UPDATE SET "
                "state='frozen',target_binding_version=excluded.target_binding_version, "
                "old_key_check=CASE WHEN state='active' THEN NULL ELSE old_key_check END, "
                "target_key_check=CASE WHEN state='active' THEN NULL ELSE target_key_check END"
            ).bind(store, environment, old, target),
            db.prepare("DELETE FROM native_guard"),
        ])
    except Exception as exc:
        raise ValueError("rotation unavailable") from exc


async def backfill(db, *, store, environment, old, target, old_key, new_key, contacts, now):
    """Append one bounded chunk of verified identities and copy existing tombstones."""
    _validate(store, environment, old, target)
    if not isinstance(contacts, list) or len(contacts) > 50 or type(now) is not int or now < 1:
        raise ValueError("rotation unavailable")
    if old_key == new_key:
        raise ValueError("rotation unavailable")
    old_version, target_version = "v2_" + old, "v2_" + target
    pairs = {(_digest(old_key, store, email), _digest(new_key, store, email)) for email in contacts}
    old_proof = _rotation_key_check(old_key, store, environment, old)
    target_proof = _rotation_key_check(new_key, store, environment, target)
    statements = [db.prepare(
        "UPDATE review_suppression_rotation SET old_key_check=COALESCE(old_key_check,?), "
        "target_key_check=COALESCE(target_key_check,?) WHERE store_key=? AND environment=? "
        "AND state='frozen' AND active_binding_version=? AND target_binding_version=?"
    ).bind(old_proof, target_proof, store, environment, old_version, target_version), db.prepare(
        "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_suppression_rotation "
        "WHERE store_key=? AND environment=? AND state='frozen' AND active_binding_version=? "
        "AND target_binding_version=? AND old_key_check=? AND target_key_check=?) THEN 1 ELSE 0 END"
    ).bind(store, environment, old_version, target_version, old_proof, target_proof)]
    for old_hash, new_hash in pairs:
        # Possession of a correct old-key preimage is checked against actual
        # immutable evidence, never accepted as a browser-supplied identity.
        statements += [
            db.prepare(
                "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_automation_recipient_bindings "
                "WHERE store_key=? AND environment=? AND recipient_context_hash=? "
                "AND (binding_version='v2' OR binding_version GLOB 'v2_*')) "
                "OR EXISTS(SELECT 1 FROM review_recipient_suppressions WHERE store_key=? AND environment=? "
                "AND recipient_context_hash=?) OR EXISTS(SELECT 1 FROM review_suppression_aliases "
                "WHERE store_key=? AND environment=? AND recipient_context_hash=?) THEN 1 ELSE 0 END"
            ).bind(store, environment, old_hash, store, environment, old_hash, store, environment, old_hash),
            db.prepare(
                "INSERT OR IGNORE INTO review_suppression_aliases VALUES (?,?,?,?,?)"
            ).bind(store, environment, old_hash, old_hash, old_version),
            db.prepare(
                "INSERT OR IGNORE INTO review_suppression_aliases "
                "SELECT store_key,environment,?,canonical_hash,? FROM review_suppression_aliases "
                "WHERE store_key=? AND environment=? AND recipient_context_hash=?"
            ).bind(new_hash, target_version, store, environment, old_hash),
            db.prepare(
                "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_suppression_aliases a "
                "JOIN review_suppression_aliases b USING(store_key,environment,canonical_hash) "
                "WHERE a.store_key=? AND a.environment=? AND a.recipient_context_hash=? "
                "AND b.recipient_context_hash=? AND b.binding_version=?) THEN 1 ELSE 0 END"
            ).bind(store, environment, old_hash, new_hash, target_version),
        ]
    statements += [
        db.prepare(
            "INSERT OR IGNORE INTO review_recipient_suppressions "
            "(store_key,environment,recipient_context_hash,purpose,created_at) "
            "SELECT a.store_key,a.environment,a.recipient_context_hash,'review_invitation',? "
            "FROM review_suppression_aliases a JOIN review_suppression_aliases b "
            "USING(store_key,environment,canonical_hash) JOIN review_recipient_suppressions s "
            "ON s.store_key=b.store_key AND s.environment=b.environment "
            "AND s.recipient_context_hash=b.recipient_context_hash AND s.purpose='review_invitation' "
            "WHERE a.store_key=? AND a.environment=?"
        ).bind(now, store, environment),
        db.prepare("DELETE FROM native_guard"),
    ]
    try:
        await db.batch(statements)
    except Exception as exc:
        raise ValueError("rotation unavailable") from exc


async def switch_context(db, context_db, *, store, environment, installation, old, target, old_key, new_key):
    """Pin and select a new key only while complete Reviews aliases are fenced.

    This is the middle of a cross-D1 transition: failure or restart leaves the
    Reviews fence in place until `activate` validates the target key again.
    """
    _validate(store, environment, old, target)
    if (not _SCOPE.fullmatch(installation or "") or not isinstance(old_key, str) or len(old_key) < 32
            or not isinstance(new_key, str) or len(new_key) < 32 or old_key == new_key):
        raise ValueError("rotation unavailable")
    ready = await db.prepare(
        "SELECT 1 AS ready WHERE " + _coverage_sql() +
        " AND EXISTS(SELECT 1 FROM review_suppression_rotation WHERE store_key=? AND environment=? "
        "AND old_key_check=? AND target_key_check=?)"
    ).bind(
        *_coverage_params(store, environment, old, target), store, environment,
        _rotation_key_check(old_key, store, environment, old),
        _rotation_key_check(new_key, store, environment, target),
    ).all()
    if len(ready.results) != 1:
        raise ValueError("rotation unavailable")
    old_check = _key_check(old_key, store, environment, installation, old)
    target_check = _key_check(new_key, store, environment, installation, target)
    try:
        await context_db.batch([
            context_db.prepare(
                "INSERT INTO review_suppression_key_guard SELECT CASE WHEN EXISTS(SELECT 1 "
                "FROM review_suppression_key_current c JOIN review_suppression_key_registry r "
                "USING(store_key,environment,installation_id,key_version) "
                "WHERE c.store_key=? AND c.environment=? AND c.installation_id=? "
                "AND ((c.key_version=? AND r.key_check=?) OR (c.key_version=? AND r.key_check=?))) "
                "THEN 1 ELSE 0 END"
            ).bind(store, environment, installation, old, old_check, target, target_check),
            context_db.prepare(
                "INSERT OR IGNORE INTO review_suppression_key_registry VALUES (?,?,?,?,?)"
            ).bind(store, environment, installation, target, target_check),
            context_db.prepare(
                "INSERT INTO review_suppression_key_guard SELECT CASE WHEN EXISTS(SELECT 1 "
                "FROM review_suppression_key_registry WHERE store_key=? AND environment=? "
                "AND installation_id=? AND key_version=? AND key_check=?) THEN 1 ELSE 0 END"
            ).bind(store, environment, installation, target, target_check),
            context_db.prepare(
                "UPDATE review_suppression_key_current SET key_version=? WHERE store_key=? AND environment=? "
                "AND installation_id=? AND key_version IN (?,?)"
            ).bind(target, store, environment, installation, old, target),
            context_db.prepare("DELETE FROM review_suppression_key_guard"),
        ])
    except Exception as exc:
        raise ValueError("rotation unavailable") from exc


async def activate(db, context_db, *, store, environment, installation, old, target, new_key):
    """Unfreeze only after the new context key is pinned and alias coverage is complete."""
    _validate(store, environment, old, target)
    if not _SCOPE.fullmatch(installation or "") or not isinstance(new_key, str) or len(new_key) < 32:
        raise ValueError("rotation unavailable")
    check = _key_check(new_key, store, environment, installation, target)
    result = await context_db.prepare(
        "SELECT r.key_check FROM review_suppression_key_registry r JOIN review_suppression_key_current c "
        "USING(store_key,environment,installation_id,key_version) WHERE r.store_key=? AND r.environment=? "
        "AND r.installation_id=? AND r.key_version=?"
    ).bind(store, environment, installation, target).all()
    rows = [x.to_py() if hasattr(x, "to_py") else dict(x) for x in result.results]
    if len(rows) != 1 or not hmac.compare_digest(rows[0]["key_check"], check):
        raise ValueError("rotation unavailable")
    target_version = "v2_" + target
    # A lost HTTP response after the final commit is safe to retry. A later
    # rotation changes the active version or proof, so stale calls cannot undo it.
    existing = await db.prepare(
        "SELECT 1 ok FROM review_suppression_rotation WHERE store_key=? AND environment=? "
        "AND state='active' AND active_binding_version=? AND target_binding_version IS NULL "
        "AND target_key_check=?"
    ).bind(store, environment, target_version, _rotation_key_check(new_key, store, environment, target)).all()
    if len(existing.results) == 1:
        return
    try:
        await db.batch([
            db.prepare(
                "INSERT INTO native_guard SELECT CASE WHEN " + _coverage_sql() +
                " AND EXISTS(SELECT 1 FROM review_suppression_rotation WHERE store_key=? AND environment=? "
                "AND target_key_check=?) THEN 1 ELSE 0 END"
            ).bind(*_coverage_params(store, environment, old, target), store, environment,
                   _rotation_key_check(new_key, store, environment, target)),
            db.prepare("UPDATE review_suppression_rotation SET state='active',active_binding_version=?, "
                       "target_binding_version=NULL WHERE store_key=? AND environment=?")
            .bind(target_version, store, environment),
            db.prepare("DELETE FROM native_guard"),
        ])
    except Exception as exc:
        raise ValueError("rotation unavailable") from exc
