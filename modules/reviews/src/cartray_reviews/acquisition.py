"""Read purchase eligibility from CartRay's confirmed settlement records."""

import re
from dataclasses import dataclass

SCOPE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


@dataclass(frozen=True)
class VerifiedAcquisition:
    """Server-derived acquisition facts; recipient delivery is a separate concern."""

    store: str
    environment: str
    acquisition_id: str
    event_id: str
    manifest_version: str
    subject_ids: tuple[str, ...]


async def confirmed_purchase(database, *, store, environment, event_id, subject_maps):
    """Read a paid, confirmed settlement and its immutable product-to-subject mapping.

    The caller must supply a trusted CartRay database and trusted, version-pinned
    subject maps. Neither the database binding nor maps may come from a request.
    This function does not infer a recipient from a payment event.
    """
    if not isinstance(store, str) or not SCOPE_ID.fullmatch(store) or environment != "test":
        raise ValueError("invalid acquisition scope")
    if not isinstance(event_id, str) or not SCOPE_ID.fullmatch(event_id):
        raise ValueError("invalid event identity")
    if event_id.startswith("recon_"):
        order_id = event_id.removeprefix("recon_")
        if not SCOPE_ID.fullmatch(order_id):
            raise ValueError("invalid reconciliation identity")
        statement = database.prepare(
            "SELECT o.order_id,o.manifest_version,i.product_key "
            "FROM checkout_sessions c JOIN checkout_reconciliations r ON r.order_id=c.order_id "
            "AND r.session_id=c.external_session_id JOIN orders o ON o.order_id=c.order_id "
            "JOIN order_items i ON i.order_id=o.order_id "
            "WHERE c.order_id=? AND c.settlement_state='confirmed' AND c.settled_at IS NOT NULL "
            "AND c.settlement_event_id IS NULL AND c.settlement_session_id=c.external_session_id "
            "AND c.stripe_payment_status='paid' AND c.stripe_amount_total_minor>0 "
            "AND r.last_outcome='confirmed' AND r.last_error IS NULL AND r.observed_status='complete' "
            "AND r.observed_payment_status=c.stripe_payment_status "
            "AND r.observed_amount_total_minor=c.stripe_amount_total_minor "
            "AND i.unit_amount_minor>0 AND i.quantity>0 ORDER BY i.product_key LIMIT 101"
        ).bind(order_id)
    else:
        statement = database.prepare(
            "SELECT o.order_id,o.manifest_version,i.product_key "
            "FROM stripe_events e JOIN checkout_sessions c ON c.settlement_event_id=e.stripe_event_id "
            "JOIN orders o ON o.order_id=c.order_id JOIN order_items i ON i.order_id=o.order_id "
            "WHERE e.stripe_event_id=? AND e.event_type='checkout.session.completed' "
            "AND e.processing_state='confirmed' AND e.processed_at IS NOT NULL AND e.processing_error IS NULL "
            "AND c.settlement_state='confirmed' AND c.settled_at IS NOT NULL "
            "AND c.external_session_id=e.session_id AND c.settlement_session_id=e.session_id "
            "AND c.stripe_payment_status='paid' AND c.stripe_amount_total_minor>0 "
            "AND i.unit_amount_minor>0 AND i.quantity>0 "
            "ORDER BY i.product_key LIMIT 101"
        ).bind(event_id)
    result = await statement.all()
    records = [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]
    if not records or len(records) > 100:
        raise ValueError("purchase unavailable")
    first = records[0]
    order_id, manifest = first["order_id"], first["manifest_version"]
    if not isinstance(order_id, str) or not SCOPE_ID.fullmatch(order_id):
        raise ValueError("invalid acquisition identity")
    mapping = subject_maps.get(manifest)
    if not isinstance(mapping, dict):
        raise ValueError("unmapped acquisition manifest")
    subjects = set()
    for item in records:
        if item["order_id"] != order_id or item["manifest_version"] != manifest:
            raise ValueError("inconsistent acquisition")
        subject = mapping.get(item["product_key"])
        if not isinstance(subject, str) or not SCOPE_ID.fullmatch(subject):
            raise ValueError("unmapped acquisition subject")
        subjects.add(subject)
    return VerifiedAcquisition(store, environment, order_id, event_id, manifest, tuple(sorted(subjects)))
