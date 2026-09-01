from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from cartray.errors import WebhookValidationError
from cartray.webhook import StripeWebhookEvent, StripeWebhookSignatureVerifier


def signed_event(payload: dict[str, object], *, timestamp: int = 1_700_000_000, secret: str = "whsec_fixture"):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={timestamp},v1={signature}"


def checkout_event() -> dict[str, object]:
    return {
        "id": "evt_fixture_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {"id": "cs_fixture_1"}},
    }


def test_stripe_webhook_signature_accepts_any_matching_v1_candidate():
    raw, signature = signed_event(checkout_event())

    StripeWebhookSignatureVerifier("whsec_fixture").verify(raw, f"v1={'0' * 64},{signature}", now=1_700_000_001)


@pytest.mark.parametrize(
    "signature,now",
    (("t=1700000000,v1=" + "0" * 64, 1_700_000_001), ("t=1700000000,v1=" + "0" * 64, 1_700_000_301)),
)
def test_stripe_webhook_signature_rejects_invalid_or_stale_delivery(signature, now):
    raw, _valid_signature = signed_event(checkout_event())

    with pytest.raises(WebhookValidationError):
        StripeWebhookSignatureVerifier("whsec_fixture").verify(raw, signature, now=now)


def test_checkout_event_keeps_a_raw_body_hash_and_session_identity():
    raw, _signature = signed_event(checkout_event())

    event = StripeWebhookEvent.from_raw(raw)

    assert event.event_id == "evt_fixture_1"
    assert event.session_id == "cs_fixture_1"
    assert event.payload_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
