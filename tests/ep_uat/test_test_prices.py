from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("uat_test_prices", ROOT / "examples/ep-uat/provision_test_prices.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_source_price_and_test_mode_are_enforced():
    catalogue = {
        "source_currency": "AUD",
        "source_archive_sha256": "a" * 64,
        "products": [
            {
                "source_id": 171,
                "source_type": "simple",
                "source_delivery_kind": "uat_dummy_transfer",
                "source_price": "1349",
                "key": "ep:woo:offer:171",
                "title": "Test product",
                "source_sku": "coremod",
                "images": [{"path": "media/image.png"}],
            }
        ],
    }
    calls = []

    def transport(key, method, path, data=None, idempotency=None):
        calls.append((method, path))
        if path == "account":
            return {"id": "acct_Test"}
        if method == "GET":
            return {"data": []}
        return {
            "id": "price_Test",
            "livemode": False,
            "active": True,
            "type": "one_time",
            "recurring": None,
            "unit_amount": 134900,
            "currency": "aud",
            "lookup_key": data["lookup_key"],
            "metadata": {
                "uat_source_key": data["metadata[uat_source_key]"],
                "uat_source_sha256": data["metadata[uat_source_sha256]"],
            },
        }

    result = module.provision(catalogue, [171], "sk_test_example", "acct_Test", transport)
    assert result["offers"][0]["amount_minor"] == 134900
    assert result["offers"][0]["source_key"] == "ep:woo:offer:171"
    assert result["offers"][0]["dummy_entitlement"] == "EP-UAT-DUMMY-171"
    assert calls[-1][0] == "POST"
    with pytest.raises(ValueError, match="test-only"):
        module.provision(catalogue, [171], "sk_live_example", "acct_Test", transport)
    catalogue["products"][0]["source_price"] = ""
    with pytest.raises(ValueError, match="price"):
        module.provision(catalogue, [171], "sk_test_example", "acct_Test", transport)


def test_invalid_second_offer_causes_no_provider_calls():
    catalogue = {
        "source_currency": "AUD",
        "source_archive_sha256": "a" * 64,
        "products": [
            {
                "source_id": 171,
                "source_type": "simple",
                "source_delivery_kind": "uat_dummy_transfer",
                "source_price": "10",
                "images": [{"path": "media/a.png"}],
            },
            {
                "source_id": 172,
                "source_type": "simple",
                "source_delivery_kind": "uat_dummy_transfer",
                "source_price": "",
                "images": [{"path": "media/b.png"}],
            },
        ],
    }
    calls = []
    with pytest.raises(ValueError, match="price"):
        module.provision(catalogue, [171, 172], "sk_test_example", "acct_Test", lambda *a, **k: calls.append(a))
    assert calls == []


def test_fractional_and_invalid_prices_are_not_guessed():
    for value in ("0", "-1", "1.001", "NaN", ""):
        with pytest.raises(ValueError):
            module.amount_minor(value)


def test_followup_fixture_is_dummy_and_never_automatically_sent():
    import json

    data = {"products": [{"source_id": 171, "key": "ep:woo:offer:171", "source_workflow_keys": ["ep:woo:workflow:1"]}]}
    workflow = {
        "key": "ep:woo:workflow:1",
        "matchingOfferKeys": ["ep:woo:offer:171"],
        "actions": [{"kind": "uat_dummy_transfer", "sendEnabled": False, "url": "https://www.dropbox.com/t/ABCDEFG"}],
    }
    blobs = {"workflows.json": json.dumps([workflow]).encode()}
    result = module.dummy_followups(blobs, data, [171])
    assert result[0]["send_enabled"] is False
    workflow["actions"][0]["sendEnabled"] = True
    with pytest.raises(ValueError, match="dummy transfer"):
        module.dummy_followups({"workflows.json": json.dumps([workflow]).encode()}, data, [171])
