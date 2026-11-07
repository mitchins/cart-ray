"""Authenticated test adapter's durable, no-send OrderConfirmed intake."""

import hashlib
import json
import re

from cartray_reviews.acquisition import confirmed_purchase

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def activate_intake(
    reviews_db, checkout_db, *, store, environment, policy_version, delay_seconds, max_lateness, now
):
    """Fence existing outbox events without modifying or acknowledging checkout.

    The committed outbox snapshot is the activation boundary. The CartRay binding
    must be provisioned by a trusted operator; it does not attest its own scope.
    """
    if (
        environment != "test"
        or not isinstance(store, str)
        or not _ID.fullmatch(store)
        or not isinstance(policy_version, str)
        or not _ID.fullmatch(policy_version)
        or type(now) is not int
        or now < 1
        or type(delay_seconds) is not int
        or not 0 <= delay_seconds <= 366 * 86400
        or type(max_lateness) is not int
        or not 0 <= max_lateness <= 366 * 86400
    ):
        raise ValueError("invalid activation")

    async def existing():
        return _rows(
            await reviews_db.prepare(
                "SELECT a.activation_epoch,a.outbox_watermark,a.delay_seconds,a.max_lateness,a.policy_version,"
                "f.fence_mode FROM review_automation_activation a "
                "LEFT JOIN review_automation_fence f ON f.store_key=a.store_key AND f.environment=a.environment "
                "WHERE a.store_key=? AND a.environment=?"
            )
            .bind(store, environment)
            .all()
        )

    def result(rows):
        if len(rows) != 1:
            raise ValueError("activation unavailable")
        row = rows[0]
        if (
            row["fence_mode"] != "outbox_snapshot"
            or row["policy_version"] != policy_version
            or row["delay_seconds"] != delay_seconds
            or row["max_lateness"] != max_lateness
        ):
            raise ValueError("activation conflict")
        return {
            "status": "active",
            "activation_epoch": row["activation_epoch"],
            "outbox_watermark": row["outbox_watermark"],
        }

    prior = await existing()
    if prior:
        return result(prior)
    # A stale read replica could re-enrol old events, so the fence must be read
    # from the authoritative primary session and must fail if unavailable.
    source = _rows(
        await checkout_db.withSession("first-primary")
        .prepare("SELECT COALESCE(MAX(id),0) AS watermark FROM outbox")
        .all()
    )
    if len(source) != 1 or type(source[0]["watermark"]) is not int or source[0]["watermark"] < 0:
        raise ValueError("source unavailable")
    watermark = source[0]["watermark"]
    try:
        await reviews_db.batch(
            [
                reviews_db.prepare("INSERT INTO review_automation_activation VALUES (?,?,?,?,?,?,?)").bind(
                    store, environment, now, watermark, delay_seconds, max_lateness, policy_version
                ),
                reviews_db.prepare("INSERT INTO review_automation_fence VALUES (?,?,?)").bind(
                    store, environment, "outbox_snapshot"
                ),
            ]
        )
    except Exception as exc:
        # A simultaneous identical activation may have committed first. Never
        # replace its watermark or silently accept different policy settings.
        prior = await existing()
        if not prior:
            raise ValueError("activation unavailable") from exc
        return result(prior)
    return result(await existing())


async def intake_order(reviews_db, checkout_db, *, store, environment, outbox_id, subject_maps, now):
    """Re-read authoritative settlement and acknowledge only after durable Reviews commit.

    The event source and both database bindings are configured server-side. Acknowledgement
    is independent of CartRay's shared outbox delivery field; this adapter never updates it.
    """
    if environment != "test" or not _ID.fullmatch(store) or type(outbox_id) is not int or outbox_id < 1:
        raise ValueError("invalid intake")
    if type(now) is not int or now < 1:
        raise ValueError("invalid clock")
    policies = _rows(
        await reviews_db.prepare(
            "SELECT a.activation_epoch,a.outbox_watermark,a.delay_seconds,a.max_lateness,a.policy_version,"
            "f.fence_mode FROM review_automation_activation a LEFT JOIN review_automation_fence f "
            "ON f.store_key=a.store_key AND f.environment=a.environment WHERE a.store_key=? AND a.environment=?"
        )
        .bind(store, environment)
        .all()
    )
    if len(policies) != 1:
        raise ValueError("intake unavailable")
    policy = policies[0]
    if not _ID.fullmatch(policy["policy_version"]):
        raise ValueError("invalid policy")
    source = _rows(
        await checkout_db.prepare(
            "SELECT b.id,b.order_id,b.payload_json,c.external_session_id,c.settlement_event_id,c.settled_at,"
            "o.items_digest FROM outbox b JOIN checkout_sessions c ON c.order_id=b.order_id "
            "JOIN orders o ON o.order_id=b.order_id WHERE b.id=? AND b.event_type='OrderConfirmed' "
            "AND c.settlement_state='confirmed' AND c.settled_at IS NOT NULL LIMIT 1"
        )
        .bind(outbox_id)
        .all()
    )
    if len(source) != 1:
        raise ValueError("confirmation unavailable")
    row = source[0]
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid confirmation") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"order_id", "session_id", "items_digest"}
        or payload["order_id"] != row["order_id"]
        or payload["session_id"] != row["external_session_id"]
        or payload["items_digest"] != row["items_digest"]
    ):
        raise ValueError("confirmation conflict")
    confirmed = row["settled_at"]
    if type(confirmed) is not int or confirmed <= 0 or confirmed > now:
        raise ValueError("invalid confirmation time")
    event = row["settlement_event_id"] or "recon_" + row["order_id"]
    acquisition = await confirmed_purchase(
        checkout_db, store=store, environment=environment, event_id=event, subject_maps=subject_maps
    )
    if acquisition.acquisition_id != row["order_id"]:
        raise ValueError("acquisition conflict")
    evidence = json.dumps(
        [
            store,
            environment,
            outbox_id,
            row["order_id"],
            event,
            confirmed,
            acquisition.manifest_version,
            acquisition.subject_ids,
            row["items_digest"],
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(evidence.encode()).hexdigest()
    due = confirmed + policy["delay_seconds"]
    if due > 253402300799:
        raise ValueError("invalid due time")
    if outbox_id <= policy["outbox_watermark"] or (
        policy["fence_mode"] != "outbox_snapshot" and confirmed <= policy["activation_epoch"]
    ):
        disposition = "pre_activation"
    elif now > due + policy["max_lateness"]:
        disposition = "late"
    else:
        disposition = "accepted"
    try:
        await reviews_db.batch(
            [
                reviews_db.prepare("INSERT OR IGNORE INTO review_automation_intake VALUES (?,?,?,?,?,?,?,?,?)").bind(
                    store,
                    environment,
                    outbox_id,
                    row["order_id"],
                    digest,
                    confirmed,
                    due,
                    policy["policy_version"],
                    disposition,
                ),
                reviews_db.prepare(
                    "INSERT OR IGNORE INTO review_automation_intents SELECT store_key,environment,acquisition_id,"
                    "outbox_id,due_at,policy_version,'held_eligibility_recipient_consent' "
                    "FROM review_automation_intake "
                    "WHERE store_key=? AND environment=? AND outbox_id=? AND disposition='accepted'"
                ).bind(store, environment, outbox_id),
            ]
        )
    except Exception as exc:
        raise ValueError("intake conflict") from exc
    persisted = _rows(
        await reviews_db.prepare(
            "SELECT disposition,due_at FROM review_automation_intake WHERE store_key=? "
            "AND environment=? AND outbox_id=?"
        )
        .bind(store, environment, outbox_id)
        .all()
    )
    return {"status": persisted[0]["disposition"], "due_at": persisted[0]["due_at"]}
