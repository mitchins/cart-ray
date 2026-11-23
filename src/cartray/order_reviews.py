"""Review solicitation is an order preference, using the normal mail provider.

Native Reviews owns review progress, capabilities and moderation. This module
only creates purchase-bound invitations and selects their presentation.
"""

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, replace

from cartray.store import _results


@dataclass(frozen=True)
class ReviewSettings:
    timing: str = "receipt"
    selection: str = "priority"
    delay_days: int = 7
    priorities: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw="{}"):
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - {"timing", "selection", "delay_days", "priorities"}:
            raise ValueError("invalid review preferences")
        if "priorities" in value and not isinstance(value["priorities"], list):
            raise ValueError("review priorities must be an ordered list")
        result = cls(**{**value, "priorities": tuple(value.get("priorities", []))})
        if (
            result.timing not in ("off", "receipt", "followup", "both")
            or result.selection not in ("all", "priority")
            or type(result.delay_days) not in (int, float)
            or not 0 <= result.delay_days <= 365
            or len(result.priorities) > 100
            or len(set(result.priorities)) != len(result.priorities)
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x) for x in result.priorities)
        ):
            raise ValueError("invalid review preferences")
        return result

    def select(self, subjects):
        ranked = sorted(
            set(subjects), key=lambda x: (self.priorities.index(x) if x in self.priorities else len(self.priorities), x)
        )
        return ranked if self.selection == "all" else ranked[:1]


async def first(db, sql, *values):
    rows = _results(await db.prepare(sql).bind(*values).all())
    return rows[0] if rows else None


def recipient_hash(secret, merchant, environment, email):
    # Preserve the existing stable pseudonym, including its domain separator.
    local, domain = email.rsplit("@", 1)
    return hmac.new(
        secret.encode(),
        json.dumps(
            ["acorn-recipient-v2", merchant, environment, local + "@" + domain.lower()],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


_PERMISSION_SQL = """SELECT g.order_id FROM review_invitation_checkout_grants g
        JOIN review_invitation_checkout_choices c USING(order_id)
        JOIN checkout_sessions settled USING(order_id)
        WHERE g.order_id=? AND settled.settlement_state='confirmed'
        AND g.granted_at>0 AND g.granted_at<=settled.settled_at AND c.selected=1 AND c.recorded_at=g.granted_at
        AND c.wording=g.wording AND c.wording_version=g.wording_version
        AND c.merchant_default_selected=g.merchant_default_selected
        AND g.selection_basis=CASE WHEN c.merchant_default_selected=1
          THEN 'preselected_acceptance' ELSE 'express_opt_in' END
        AND NOT EXISTS(SELECT 1 FROM review_recipient_suppressions
          WHERE store_key=? AND environment=? AND recipient_context_hash=? AND purpose='review_invitation')
        AND NOT EXISTS(SELECT 1 FROM review_invitation_consents v
          WHERE v.store_key=? AND v.environment=? AND v.acquisition_id=? 
          AND (v.revoked_at IS NOT NULL OR v.recipient_context_hash<>?))
        AND NOT EXISTS(SELECT 1 FROM review_invitation_consent_withdrawals
          WHERE store_key=? AND environment=? AND acquisition_id=? AND recipient_context_hash=?)"""


def permission_values(merchant, environment, order, recipient):
    return (
        order,
        merchant,
        environment,
        recipient,
        merchant,
        environment,
        order,
        recipient,
        merchant,
        environment,
        order,
        recipient,
    )


async def permitted(db, merchant, environment, order, recipient):
    return bool(await first(db, _PERMISSION_SQL, *permission_values(merchant, environment, order, recipient)))


async def outstanding(db, merchant, environment, order):
    rows = _results(
        await db.prepare("""SELECT s.subject_id FROM acquisition_invitation_subjects s
        JOIN acquisition_invitations i USING(store_key,environment,acquisition_id)
        WHERE s.store_key=? AND s.environment=? AND s.acquisition_id=? AND s.consumed_at IS NULL
        AND i.revoked_at IS NULL AND (i.expires_at=-1 OR i.expires_at>unixepoch())
        ORDER BY s.subject_id""")
        .bind(merchant, environment, order)
        .all()
    )
    return [row["subject_id"] for row in rows]


async def prepare(
    db, *, merchant, environment, order, email, hash_secret, key_version, installation, subject_map, settings, now
):
    """Caller supplies an independently verified Stripe recipient, never browser fields."""
    if environment != "test" or len(hash_secret) < 32 or settings.timing == "off":
        return None
    check = hmac.new(
        hash_secret.encode(),
        json.dumps(
            ["reviews-suppression-key-check-v1", merchant, environment, installation, key_version],
            separators=(",", ":"),
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    pinned = await first(
        db,
        """SELECT r.key_check FROM review_suppression_key_registry r
        JOIN review_suppression_key_current c USING(store_key,environment,installation_id,key_version)
        WHERE r.store_key=? AND r.environment=? AND r.installation_id=? AND r.key_version=?""",
        merchant,
        environment,
        installation,
        key_version,
    )
    if not pinned or not hmac.compare_digest(pinned["key_check"], check):
        raise ValueError("restore the installed recipient suppression key")
    recipient = recipient_hash(hash_secret, merchant, environment, email)
    if not await permitted(db, merchant, environment, order, recipient):
        return None
    purchase = await first(
        db,
        """SELECT s.settlement_event_id,s.settled_at,o.manifest_version
        FROM checkout_sessions s JOIN orders o USING(order_id)
        WHERE s.order_id=? AND s.settlement_state='confirmed' AND s.stripe_payment_status='paid'
        AND s.stripe_amount_total_minor>0 AND EXISTS(SELECT 1 FROM outbox e
          JOIN post_purchase_checkpoint p ON p.singleton=1
          WHERE e.order_id=s.order_id AND e.event_type='OrderConfirmed' AND e.id>p.activation_watermark
          AND p.merchant_id=? AND p.environment=?)""",
        order,
        merchant,
        environment,
    )
    if not purchase:
        return None
    rows = _results(
        await db.prepare("SELECT product_key FROM order_items WHERE order_id=? AND unit_amount_minor>0")
        .bind(order)
        .all()
    )
    mapping = subject_map.get(purchase["manifest_version"], {})
    subjects = sorted({mapping[row["product_key"]] for row in rows if row["product_key"] in mapping})
    if not subjects:
        return None
    existing_invitation = await first(
        db,
        "SELECT token_hash FROM acquisition_invitations WHERE store_key=? AND environment=? AND acquisition_id=?",
        merchant,
        environment,
        order,
    )
    existing_message = await first(
        db,
        "SELECT invitation_token FROM order_review_messages WHERE merchant_id=? AND environment=? AND order_id=?",
        merchant,
        environment,
        order,
    )
    if existing_invitation and (
        not existing_message
        or existing_invitation["token_hash"]
        != hashlib.sha256(existing_message["invitation_token"].encode()).hexdigest()
    ):
        return None
    grant = await first(db, 'SELECT wording_version FROM review_invitation_checkout_grants WHERE order_id=?', order)
    if settings.timing == 'both' and grant['wording_version'] != 'review-invitation-v2':
        settings = replace(settings, timing='receipt')
    settings = replace(settings, priorities=tuple(dict.fromkeys(mapping.get(key, key) for key in settings.priorities)))
    # Insert once; concurrent attempts recover the winning unpredictable token.
    token, unsubscribe = secrets.token_hex(32), secrets.token_hex(32)
    saved_settings = json.dumps(settings.__dict__, separators=(",", ":"))
    due = now + round(settings.delay_days * 86400) if settings.timing in ("followup", "both") else None
    statements = [
        db.prepare("""INSERT OR IGNORE INTO order_review_messages
        (merchant_id,environment,order_id,recipient_hash,invitation_token,unsubscribe_token,
         settings_json,due_at,state,created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?)""").bind(
            merchant, environment, order, recipient, token, unsubscribe, saved_settings, due, now
        ),
        db.prepare("""INSERT OR IGNORE INTO review_invitation_consents
          (store_key,environment,acquisition_id,recipient_context_hash,purpose,evidence_reference,granted_at)
          SELECT ?,?,?,?,'review_invitation','checkout:'||order_id,granted_at
          FROM review_invitation_checkout_grants WHERE order_id=?""").bind(
            merchant, environment, order, recipient, order
        ),
    ]
    statements.append(
        db.prepare("""INSERT OR IGNORE INTO acquisition_invitations
          (store_key,environment,acquisition_id,source_event_id,recipient_context_hash,subject_set_hash,
           token_hash,expires_at,key_version) VALUES (?,?,?,?,?,?,?,-1,'v1')""").bind(
            merchant,
            environment,
            order,
            purchase["settlement_event_id"] or "recon_" + order,
            recipient,
            hashlib.sha256(json.dumps(subjects, separators=(",", ":")).encode()).hexdigest(),
            hashlib.sha256(token.encode()).hexdigest(),
        )
    )
    statements.append(
        db.prepare("INSERT INTO native_guard SELECT CASE WHEN EXISTS(" + _PERMISSION_SQL + ") THEN 1 ELSE 0 END").bind(
            *permission_values(merchant, environment, order, recipient)
        )
    )
    # A guard makes the capability and message winner atomic under concurrency.
    statements.insert(
        1,
        db.prepare("""INSERT INTO native_guard SELECT CASE WHEN EXISTS(
        SELECT 1 FROM order_review_messages WHERE merchant_id=? AND environment=? AND order_id=?
        AND invitation_token=?) THEN 1 ELSE 0 END""").bind(merchant, environment, order, token),
    )
    statements.append(
        db.prepare("""INSERT INTO native_guard SELECT CASE WHEN EXISTS(
        SELECT 1 FROM acquisition_invitations WHERE store_key=? AND environment=? AND acquisition_id=?
        AND token_hash=? AND recipient_context_hash=? AND revoked_at IS NULL) THEN 1 ELSE 0 END""").bind(
            merchant, environment, order, hashlib.sha256(token.encode()).hexdigest(), recipient
        )
    )
    for subject in subjects:
        statements.append(db.prepare("""INSERT INTO native_guard SELECT CASE WHEN EXISTS(
            SELECT 1 FROM review_subjects s JOIN review_subject_display_names n USING(store_key,environment,subject_id)
            WHERE s.store_key=? AND s.environment=? AND s.subject_id=?) THEN 1 ELSE 0 END""")
            .bind(merchant, environment, subject))
        statements.append(
            db.prepare("""INSERT INTO acquisition_invitation_subjects
          (store_key,environment,acquisition_id,subject_id) SELECT ?,?,?,?
          WHERE EXISTS(SELECT 1 FROM review_subjects WHERE store_key=? AND environment=? AND subject_id=?)
          ON CONFLICT DO NOTHING""").bind(merchant, environment, order, subject, merchant, environment, subject)
        )
    statements.append(db.prepare("DELETE FROM native_guard"))
    existing = await first(
        db,
        "SELECT * FROM order_review_messages WHERE merchant_id=? AND environment=? AND order_id=?",
        merchant,
        environment,
        order,
    )
    if not existing:
        try:
            await db.batch(statements)
        except Exception:
            existing = await first(
                db,
                "SELECT * FROM order_review_messages WHERE merchant_id=? AND environment=? AND order_id=?",
                merchant,
                environment,
                order,
            )
            if not existing:
                raise
    message = await first(
        db,
        "SELECT * FROM order_review_messages WHERE merchant_id=? AND environment=? AND order_id=?",
        merchant,
        environment,
        order,
    )
    invitation = await first(
        db,
        "SELECT token_hash,revoked_at FROM acquisition_invitations "
        "WHERE store_key=? AND environment=? AND acquisition_id=?",
        merchant,
        environment,
        order,
    )
    if (
        not invitation
        or invitation["revoked_at"] is not None
        or invitation["token_hash"] != hashlib.sha256(message["invitation_token"].encode()).hexdigest()
    ):
        return None  # Preserve previously issued generations rather than replacing them.
    return message


async def unsubscribe(db, merchant, environment, token, now):
    if not isinstance(token, str) or not re.fullmatch("[a-f0-9]{64}", token):
        raise ValueError("unavailable")
    row = await first(
        db,
        "SELECT order_id,recipient_hash FROM order_review_messages "
        "WHERE merchant_id=? AND environment=? AND unsubscribe_token=?",
        merchant,
        environment,
        token,
    )
    if not row:
        raise ValueError("unavailable")
    await db.batch(
        [
            db.prepare(
                "UPDATE order_review_messages SET state='skipped' WHERE merchant_id=? "
                "AND environment=? AND recipient_hash=? AND state='pending'"
            ).bind(merchant, environment, row["recipient_hash"]),
            db.prepare("INSERT OR IGNORE INTO review_recipient_suppressions VALUES (?,?,?,'review_invitation',?)").bind(
                merchant, environment, row["recipient_hash"], now
            ),
            db.prepare(
                "UPDATE review_invitation_consents SET revoked_at=COALESCE(revoked_at,?) "
                "WHERE store_key=? AND environment=? AND acquisition_id=?"
            ).bind(now, merchant, environment, row["order_id"]),
        ]
    )


async def claim_followup(db, message, selected=None):
    """Permission, current capability and selected progress are checked at the claim."""
    merchant, environment, order, recipient = (
        message[k] for k in ("merchant_id", "environment", "order_id", "recipient_hash")
    )
    selected = json.dumps(selected if selected is not None else json.loads(message.get("selection_json") or "[]"))
    result = (
        await db.prepare(
            """UPDATE order_review_messages SET state='claimed',selection_json=?,attempted_at=unixepoch()
        WHERE merchant_id=? AND environment=? AND order_id=? AND state='pending'
        AND EXISTS("""
            + _PERMISSION_SQL
            + """)
        AND EXISTS(SELECT 1 FROM acquisition_invitations i WHERE i.store_key=? AND i.environment=?
          AND i.acquisition_id=? AND i.recipient_context_hash=? AND i.token_hash=?
          AND i.revoked_at IS NULL AND (i.expires_at=-1 OR i.expires_at>unixepoch()))
        AND json_array_length(?)>0
        AND NOT EXISTS(SELECT 1 FROM json_each(?) chosen
          WHERE NOT EXISTS(SELECT 1 FROM acquisition_invitation_subjects s WHERE s.store_key=?
            AND s.environment=? AND s.acquisition_id=? AND s.subject_id=chosen.value AND s.consumed_at IS NULL))"""
        )
        .bind(
            selected,
            merchant,
            environment,
            order,
            *permission_values(merchant, environment, order, recipient),
            merchant,
            environment,
            order,
            recipient,
            hashlib.sha256(message["invitation_token"].encode()).hexdigest(),
            selected,
            selected,
            merchant,
            environment,
            order,
        )
        .run()
    )
    from cartray.store import _changes

    return _changes(result) == 1
