from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/m11b_projection_acceptance.py"
CATALOGUE_VERSION = "sha256:8d3674ae409653d9194f71fcdeb5f7b24f4d8442aac3fd966d0c2612aeba71ee"


def test_acceptance_proof_uses_deployed_profile_and_stripe_session_not_d1(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    requests = []
    verifications = []

    def request(method, url, *, headers=None, body=None):
        requests.append((method, url, headers, body))
        if url.endswith("/catalogue"):
            return {"version": CATALOGUE_VERSION, "products": [{"product_key": "EP-SIL-2026", "amount_minor": 85000}]}
        if url.endswith("/checkout"):
            payload = json.loads(body)
            assert payload["items"] == [{"product_key": "EP-SIL-2026", "quantity": 1}]
            return {"session_id": "cs_test_real_fixture"}
        if url.endswith("/cs_test_real_fixture"):
            return {"id": "cs_test_real_fixture", "livemode": False, "mode": "payment", "allow_promotion_codes": True,
                    "metadata": {"cr_catalogue_version": CATALOGUE_VERSION, "cr_order_id": "cr_fixture",
                                 "cr_kid": "cartray-test-2026-09-01"}}
        if "/line_items?" in url:
            return {"has_more": False, "data": [{"quantity": 1, "price": {
                "id": "price_1UBhgmBLB2xuBxraTe0ota3h", "unit_amount": 85000, "currency": "aud"}}]}
        raise AssertionError(url)

    def node_verify(args, *, input, **_kwargs):
        verifications.append((args, json.loads(input)))
        return SimpleNamespace(returncode=0, stdout='{"items":[["EP-SIL-2026",1]]}')

    module["prove"].__globals__["request_json"] = request
    monkeypatch.setattr(module["prove"].__globals__["subprocess"], "run", node_verify)
    result = module["prove"](stripe_key="rk_test_fixture", public_key_raw_b64url="public-fixture")

    assert result["ed25519_verified"] is True
    assert result["checkout_completed"] is False
    assert len(requests) == 4
    assert verifications[0][1]["session_id"] == "cs_test_real_fixture"
    assert verifications[0][1]["trusted_public_keys"] == {"cartray-test-2026-09-01": "public-fixture"}


def test_acceptance_rejects_validly_signed_items_that_differ_from_stripe_line(monkeypatch):
    module = runpy.run_path(str(SCRIPT))

    def request(_method, url, **_kwargs):
        if url.endswith("/catalogue"):
            return {"version": CATALOGUE_VERSION, "products": [{"product_key": "EP-SIL-2026", "amount_minor": 85000}]}
        if url.endswith("/checkout"):
            return {"session_id": "cs_test_real_fixture"}
        if url.endswith("/cs_test_real_fixture"):
            return {"id": "cs_test_real_fixture", "livemode": False, "mode": "payment", "allow_promotion_codes": True,
                    "metadata": {"cr_catalogue_version": CATALOGUE_VERSION, "cr_order_id": "cr_fixture",
                                 "cr_kid": "cartray-test-2026-09-01"}}
        if "/line_items?" in url:
            return {"has_more": False, "data": [{"quantity": 1, "price": {
                "id": "price_1UBhgmBLB2xuBxraTe0ota3h", "unit_amount": 85000, "currency": "aud"}}]}
        raise AssertionError(url)

    module["prove"].__globals__["request_json"] = request
    monkeypatch.setattr(module["prove"].__globals__["subprocess"], "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout='{"items":[["EP-LMS-TRAINING-CATALOGUE",1]]}'))
    with pytest.raises(module["AcceptanceError"], match="differ from the Stripe line"):
        module["prove"](stripe_key="rk_test_fixture", public_key_raw_b64url="public-fixture")


def test_acceptance_proof_refuses_non_test_stripe_key_before_network():
    module = runpy.run_path(str(SCRIPT))
    with pytest.raises(module["AcceptanceError"], match="test key"):
        module["prove"](stripe_key="sk_live_invalid", public_key_raw_b64url="public-fixture")
