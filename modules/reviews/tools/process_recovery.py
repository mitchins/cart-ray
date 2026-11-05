"""Process one synthetic recovery request using the local mailbox only.

The caller supplies an authenticated API client and a trusted acquisition-to-
recipient resolver. This module never accepts a recipient from the public request.
"""

import hashlib
import hmac
import time

from test_mailbox import deliver_bound_invitation


def process_one(
    post, *, issuer, delivery_token, resolve_recipient, mailbox_path, store_name, landing_url, clock=time.time
):
    """Process one pending item without blindly retrying a claimed delivery."""
    item = post("/reviews-internal/v1/invitations/recovery-next", {}, issuer)["pending"]
    if item is None:
        return {"status": "empty"}
    recipient = resolve_recipient(item["acquisition_id"])
    # A missing lookup result can mean a temporarily unavailable recipient
    # adapter. Leave the durable request pending for a safe retry.
    if recipient is None:
        return {"status": "recipient temporarily unavailable"}
    if not isinstance(recipient, str) or not hmac.compare_digest(
        hashlib.sha256(recipient.encode()).hexdigest(), item["recipient_context_hash"]
    ):
        post("/reviews-internal/v1/invitations/recovery-cancel", {"request_id": item["request_id"]}, issuer)
        return {"status": "recipient unavailable"}
    draft = post(
        "/reviews-internal/v1/invitations/recovery-prepare",
        {"event_id": item["source_event_id"], "request_id": item["request_id"]},
        issuer,
    )
    if draft.get("state") != "prepared" or "token" not in draft:
        return {"status": "reconciliation required"}
    claim = {
        "acquisition_id": item["acquisition_id"],
        "generation_id": draft["generation_id"],
        "delivery_identity": draft["delivery_identity"],
    }
    # If the claim response is lost, do not retry sending: its persisted state
    # must be reconciled with the provider before this identity can be reused.
    result = post("/reviews-internal/v1/invitations/delivery-claim", claim, delivery_token)
    if result.get("state") != "dispatching":
        return {"status": "reconciliation required"}
    try:
        deliver_bound_invitation(
            mailbox_path,
            acquisition_id=item["acquisition_id"],
            expected_recipient_hash=item["recipient_context_hash"],
            resolve_recipient=resolve_recipient,
            store_name=store_name,
            landing_url=landing_url,
            token=draft["token"],
            delivery_identity=draft["delivery_identity"],
        )
    except Exception:
        post("/reviews-internal/v1/invitations/delivery-outcome", claim | {"outcome": "uncertain"}, delivery_token)
        return {"status": "reconciliation required"}
    accepted_at = int(clock())
    try:
        result = post(
            "/reviews-internal/v1/invitations/delivery-outcome",
            claim | {"outcome": "accepted", "accepted_at": accepted_at},
            delivery_token,
        )
    except Exception:
        return {"status": "reconciliation required"}
    return {"status": result["state"]}
