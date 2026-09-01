from __future__ import annotations

import hashlib
import hmac
import json
import re
import string
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import WebhookValidationError

MAX_WEBHOOK_BODY_BYTES = 1_048_576
DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300
_STRIPE_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9_]+$")
_STRIPE_SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class StripeWebhookEvent:
    event_id: str
    event_type: str
    livemode: bool
    session_id: str | None
    payload: dict[str, object]
    payload_sha256: str

    @classmethod
    def from_raw(cls, raw_body: bytes) -> StripeWebhookEvent:
        if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
            raise WebhookValidationError("Stripe webhook body is too large")
        try:
            payload = json.loads(raw_body)
            event_id = payload["id"]
            event_type = payload["type"]
            livemode = payload["livemode"]
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise WebhookValidationError("Stripe webhook event is invalid") from error
        if (
            not isinstance(payload, dict)
            or not isinstance(event_id, str)
            or not _STRIPE_EVENT_ID_RE.fullmatch(event_id)
        ):
            raise WebhookValidationError("Stripe webhook event ID is invalid")
        if not isinstance(event_type, str) or not isinstance(livemode, bool):
            raise WebhookValidationError("Stripe webhook event is invalid")
        session_id = _event_session_id(payload)
        payload_sha256 = "sha256:" + hashlib.sha256(raw_body).hexdigest()
        return cls(event_id, event_type, livemode, session_id, payload, payload_sha256)


@dataclass(frozen=True)
class StripeWebhookSignatureVerifier:
    endpoint_secret: str
    tolerance_seconds: int = DEFAULT_SIGNATURE_TOLERANCE_SECONDS

    def verify(self, raw_body: bytes, signature_header: str | None, *, now: int | None = None) -> None:
        if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
            raise WebhookValidationError("Stripe webhook body is too large")
        if not self.endpoint_secret:
            raise WebhookValidationError("Stripe webhook secret is unavailable")
        timestamp, signatures = _signature_parts(signature_header)
        now = int(time.time()) if now is None else now
        if abs(now - timestamp) > self.tolerance_seconds:
            raise WebhookValidationError("Stripe webhook signature timestamp is stale")
        signed_payload = str(timestamp).encode("ascii") + b"." + raw_body
        expected = hmac.new(self.endpoint_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise WebhookValidationError("Stripe webhook signature is invalid")


def _signature_parts(header: str | None) -> tuple[int, tuple[str, ...]]:
    if not isinstance(header, str):
        raise WebhookValidationError("Stripe-Signature is required")
    fields: dict[str, list[str]] = {}
    for part in header.split(","):
        key, separator, value = part.partition("=")
        if not separator or not key or not value:
            raise WebhookValidationError("Stripe-Signature is invalid")
        fields.setdefault(key, []).append(value)
    try:
        timestamp_values = fields["t"]
        timestamp = int(timestamp_values[0])
        signatures = tuple(fields["v1"])
    except (KeyError, ValueError) as error:
        raise WebhookValidationError("Stripe-Signature is invalid") from error
    if (
        len(timestamp_values) != 1
        or timestamp < 0
        or not signatures
        or any(
            len(signature) != 64 or any(character not in string.hexdigits for character in signature)
            for signature in signatures
        )
    ):
        raise WebhookValidationError("Stripe-Signature is invalid")
    return timestamp, signatures


def _event_session_id(payload: Mapping[str, object]) -> str | None:
    if payload.get("type") != "checkout.session.completed":
        return None
    try:
        session_id = payload["data"]["object"]["id"]
    except (KeyError, TypeError) as error:
        raise WebhookValidationError("Stripe checkout event has no Session ID") from error
    if not isinstance(session_id, str) or not _STRIPE_SESSION_ID_RE.fullmatch(session_id):
        raise WebhookValidationError("Stripe checkout event has an invalid Session ID")
    return session_id
