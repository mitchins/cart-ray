from __future__ import annotations

import asyncio
import runpy
from pathlib import Path

import pytest

from cartray.errors import CatalogueValidationError
from cartray.stripe import StripeApiError

SCRIPT = Path(__file__).parents[1] / "scripts" / "provision_real_test_subset.py"


class RecordingClient:
    def __init__(self, prices: list[dict[str, object]], created: dict[str, object] | None = None) -> None:
        self.prices = prices
        self.created = created
        self.posts: list[tuple[str, dict[str, str], str | None]] = []

    async def get(self, _path: str, _query: dict[str, str]) -> dict[str, object]:
        return {"data": self.prices}

    async def post(self, path: str, form: dict[str, str], *, idempotency_key: str | None) -> dict[str, object]:
        self.posts.append((path, form, idempotency_key))
        assert self.created is not None
        return self.created


def valid_price(spec) -> dict[str, object]:
    return {
        "id": "price_test_subset",
        "object": "price",
        "active": True,
        "livemode": False,
        "type": "one_time",
        "billing_scheme": "per_unit",
        "custom_unit_amount": None,
        "recurring": None,
        "currency": "aud",
        "unit_amount": spec.amount_minor,
        "lookup_key": spec.lookup_key,
    }


def test_provision_reuses_an_exact_existing_test_price():
    module = runpy.run_path(str(SCRIPT))
    spec = module["SPECS"][0]
    client = RecordingClient([valid_price(spec)])

    result = asyncio.run(module["provision"](client, spec))

    assert result == {"product_key": "EP-SIL-2026", "price_id": "price_test_subset", "status": "existing"}
    assert client.posts == []


def test_provision_creates_only_the_expected_one_time_test_price():
    module = runpy.run_path(str(SCRIPT))
    spec = module["SPECS"][1]
    client = RecordingClient([], valid_price(spec))

    result = asyncio.run(module["provision"](client, spec))

    assert result == {
        "product_key": "EP-LMS-TRAINING-CATALOGUE",
        "price_id": "price_test_subset",
        "status": "created",
    }
    assert client.posts == [
        (
            "/v1/prices",
            {
                "active": "true",
                "currency": "aud",
                "lookup_key": "cr_test_ep_lms_training_catalogue",
                "metadata[cr_product_key]": "EP-LMS-TRAINING-CATALOGUE",
                "product_data[active]": "true",
                "product_data[metadata][cr_product_key]": "EP-LMS-TRAINING-CATALOGUE",
                "product_data[name]": "LMS Training Catalogue",
                "unit_amount": "0",
            },
            "cartray-m8c-test-price:EP-LMS-TRAINING-CATALOGUE",
        )
    ]


def test_provision_rejects_incompatible_or_live_prices_and_non_test_credentials():
    module = runpy.run_path(str(SCRIPT))
    spec = module["SPECS"][0]
    incompatible = {**valid_price(spec), "livemode": True}

    with pytest.raises(StripeApiError, match="incompatible"):
        asyncio.run(module["provision"](RecordingClient([incompatible]), spec))
    with pytest.raises(CatalogueValidationError, match="test_"):
        module["require_test_secret_key"]({"STRIPE_API_KEY": "sk_live_not_allowed"})
