"""Process one synthetic recovery request using the local mailbox only.

The caller supplies an authenticated API client and a trusted acquisition-to-
recipient resolver. This module never accepts a recipient from the public request.
"""

import hashlib
import hmac
import time

from test_mailbox import deliver_bound_invitation


def process_one(
    post,
    *,
    issuer,
    delivery_token,
    resolve_recipient,
    mailbox_path,
    store_name,
    landing_url,
    clock=time.time,
    sleep=time.sleep,
):
    """Process one pending item without blindly retrying a claimed delivery.

    The authenticated post adapter must enforce a bounded network deadline.
    """
    item = post("/reviews-internal/v1/invitations/recovery-next", {}, issuer)["pending"]
    if item is None:
        return {"status": "empty"}
    try:
        recipient = resolve_recipient(item["acquisition_id"])
    except Exception:
        # Provider outages must not permanently discard a customer's request.
        recipient = None
    # A missing lookup result can mean a temporarily unavailable recipient
    # adapter. Leave the durable request pending for a safe retry.
    if recipient is None:
        post("/reviews-internal/v1/invitations/recovery-defer", {"request_id": item["request_id"]}, issuer)
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
        # The adapter cannot prove whether any particular exception occurred
        # before acceptance. Hold for authoritative reconciliation; never resend.
        post("/reviews-internal/v1/invitations/delivery-outcome", claim | {"outcome": "uncertain"}, delivery_token)
        return {"status": "reconciliation required"}
    accepted_at = int(clock())
    for attempt in range(3):
        try:
            result = post(
                "/reviews-internal/v1/invitations/delivery-outcome",
                claim | {"outcome": "accepted", "accepted_at": accepted_at},
                delivery_token,
            )
            return {"status": result["state"]}
        except Exception:
            if attempt < 2:
                sleep(1)
    return {"status": "reconciliation required"}


def reconcile_delivery(post, *, delivery_token, claim, lookup):
    """Apply an authoritative provider receipt without ever resending a message.

    Unknown or unavailable provider status remains unresolved. The provider
    must return the original acceptance time, not the time of reconciliation.
    """
    try:
        receipt = lookup(claim["delivery_identity"])
    except Exception:
        return {"status": "reconciliation required"}
    if not isinstance(receipt, dict) or receipt.get("delivery_identity") != claim["delivery_identity"]:
        return {"status": "reconciliation required"}
    state = receipt.get("status")
    if state == "accepted" and type(receipt.get("accepted_at")) is int:
        outcome = {"outcome": "accepted", "accepted_at": receipt["accepted_at"]}
    elif state == "failed" and receipt.get("final") is True:
        outcome = {"outcome": "failed"}
    else:
        return {"status": "reconciliation required"}
    try:
        result = post("/reviews-internal/v1/invitations/delivery-outcome", claim | outcome, delivery_token)
    except Exception:
        return {"status": "reconciliation required"}
    return {"status": result["state"]}
