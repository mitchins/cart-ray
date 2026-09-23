"""Create one CartRay test Checkout Session and verify its Stripe copy independently.

Requires STRIPE_API_KEY (test-only) and CARTRAY_TEST_PUBLIC_KEY_RAW_B64URL.
No checkout is completed and no customer or credential data is recorded.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from cartray.stripe import STRIPE_API_VERSION

WORKER_URL = "https://cartray-test.mitch-336.workers.dev"
CATALOGUE_LOCK = Path(__file__).parents[1] / "catalogue/real-test-subset/stripe-test-preflight.lock.json"
NODE_VERIFIER = Path(__file__).with_name("verify_m11a_projection.mjs")
SESSION_ID_RE = re.compile(r"^cs_test_[A-Za-z0-9_]+$")
EXPECTED_TEST_KID = "cartray-test-2026-09-01"
HARNESS_USER_AGENT = "CartRay-M11b-Acceptance/1.0 (+https://github.com/mitchins/cart-ray)"


class AcceptanceError(RuntimeError):
    """The test checkout did not meet the frozen projection contract."""


def request_json(method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None) -> dict:
    request_headers = dict(headers or {})
    if not any(name.lower() == "user-agent" for name in request_headers):
        request_headers["User-Agent"] = HARNESS_USER_AGENT
    request = Request(url, method=method, data=body, headers=request_headers)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise AcceptanceError("response exceeds size bound")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise AcceptanceError("response was not an object")
            return parsed
    except (HTTPError, URLError, ValueError) as error:
        raise AcceptanceError(f"{method} request failed at the test endpoint") from error


def prove(*, stripe_key: str, public_key_raw_b64url: str) -> dict[str, object]:
    if not stripe_key.startswith(("rk_test_", "sk_test_")):
        raise AcceptanceError("only a Stripe test key is accepted")
    if not isinstance(public_key_raw_b64url, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", public_key_raw_b64url):
        raise AcceptanceError("the trusted 32-byte CartRay test public key is required")
    try:
        decoded_key = base64.urlsafe_b64decode(public_key_raw_b64url + "=")
    except (binascii.Error, ValueError) as error:
        raise AcceptanceError("the trusted CartRay test public key is not canonical base64url") from error
    if len(decoded_key) != 32 or base64.urlsafe_b64encode(decoded_key).rstrip(b"=").decode() != public_key_raw_b64url:
        raise AcceptanceError("the trusted CartRay test public key is not canonical base64url")
    lock = json.loads(CATALOGUE_LOCK.read_text())
    catalogue = request_json("GET", f"{WORKER_URL}/catalogue")
    if catalogue.get("version") != lock["catalogue_version"]:
        raise AcceptanceError("deployed CartRay catalogue differs from the frozen test profile")
    product = next((item for item in catalogue.get("products", []) if item.get("product_key") == "EP-SIL-2026"), None)
    if product is None or product.get("amount_minor") != 85000:
        raise AcceptanceError("deployed SIL product is not the frozen paid test item")
    checkout_body = json.dumps(
        {
            "checkout_request_id": f"m11b-projection-{uuid4().hex}",
            "manifest_version": lock["catalogue_version"],
            "items": [{"product_key": "EP-SIL-2026", "quantity": 1}],
        }
    ).encode()
    checkout = request_json(
        "POST",
        f"{WORKER_URL}/checkout",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://cartray-store-test.pages.dev",
        },
        body=checkout_body,
    )
    session_id = checkout.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise AcceptanceError("CartRay did not return a test Checkout Session ID")
    stripe_headers = {
        "Authorization": "Basic " + base64.b64encode(f"{stripe_key}:".encode()).decode(),
        "Stripe-Version": STRIPE_API_VERSION,
    }
    session = request_json("GET", f"https://api.stripe.com/v1/checkout/sessions/{session_id}", headers=stripe_headers)
    line_items = request_json(
        "GET",
        f"https://api.stripe.com/v1/checkout/sessions/{session_id}/line_items?" + urlencode({"limit": "100"}),
        headers=stripe_headers,
    )
    if session.get("id") != session_id or session.get("livemode") is not False or session.get("mode") != "payment":
        raise AcceptanceError("Stripe returned a mismatched or non-test Session")
    if session.get("allow_promotion_codes") is not True:
        raise AcceptanceError("paid Checkout did not allow promotion codes")
    expected_price = next(item for item in lock["prices"] if item["amount_minor"] == 85000)
    lines = line_items.get("data")
    if line_items.get("has_more") is not False or not isinstance(lines, list) or len(lines) != 1:
        raise AcceptanceError("Stripe line-item projection is incomplete")
    line = lines[0]
    price = line.get("price") if isinstance(line, dict) else None
    if (
        not isinstance(line, dict)
        or line.get("quantity") != 1
        or not isinstance(price, dict)
        or price.get("id") != expected_price["stripe_price_id"]
        or price.get("unit_amount") != 85000
        or price.get("currency") != "aud"
    ):
        raise AcceptanceError("Stripe line item differs from the frozen paid catalogue")
    metadata = session.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("cr_catalogue_version") != lock["catalogue_version"]:
        raise AcceptanceError("Stripe Session lacks the frozen CartRay projection")
    if metadata.get("cr_kid") != EXPECTED_TEST_KID:
        raise AcceptanceError("Stripe Session uses an unexpected CartRay test signing key")
    verified = subprocess.run(
        ["node", str(NODE_VERIFIER)],
        input=json.dumps(
            {
                "session_id": session_id,
                "configured_environment": "test",
                "metadata": metadata,
                "trusted_public_keys": {EXPECTED_TEST_KID: public_key_raw_b64url},
            }
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if verified.returncode != 0:
        raise AcceptanceError("independent Ed25519 projection verification failed")
    try:
        signed_items = json.loads(verified.stdout)["items"]
    except (ValueError, KeyError, TypeError) as error:
        raise AcceptanceError("independent verifier did not return canonical items") from error
    if signed_items != [["EP-SIL-2026", 1]]:
        raise AcceptanceError("signed CartRay items differ from the Stripe line item")
    return {
        "session_id": session_id,
        "order_id": metadata["cr_order_id"],
        "catalogue_version": lock["catalogue_version"],
        "product_key": "EP-SIL-2026",
        "stripe_price_id": expected_price["stripe_price_id"],
        "promotion_codes_allowed": True,
        "ed25519_verified": True,
        "checkout_completed": False,
    }


def main() -> None:
    try:
        result = prove(
            stripe_key=os.environ.get("STRIPE_API_KEY", ""),
            public_key_raw_b64url=os.environ.get("CARTRAY_TEST_PUBLIC_KEY_RAW_B64URL", ""),
        )
    except AcceptanceError as error:
        print(f"M11b projection proof failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
