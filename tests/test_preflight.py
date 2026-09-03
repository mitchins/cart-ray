from __future__ import annotations

import asyncio
import json
import runpy
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from cartray import preflight
from cartray.errors import CatalogueValidationError
from cartray.preflight import (
    PreflightLock,
    StripeTestPreflightPriceResolver,
    preflight_catalogue,
    preflight_lock_output,
    preflight_output,
)
from cartray.stripe import StripeApiClient


@dataclass
class RecordingTransport:
    responses: list[tuple[int, dict[str, object]]]
    requests: list[tuple[str, str, dict[str, str], str | None]] = field(default_factory=list)

    async def request(self, method, path, *, headers, body=None):
        self.requests.append((method, path, dict(headers), body))
        return self.responses.pop(0)


def stripe_price(lookup_key: str) -> dict[str, object]:
    return {
        "id": f"price_test_{lookup_key.removeprefix('cr_test_')}",
        "object": "price",
        "unit_amount": {
            "cr_test_template": 2500,
            "cr_test_bundle": 5000,
            "cr_test_free": 0,
            "cr_test_support_hours": 10000,
        }[lookup_key],
        "currency": "aud",
        "recurring": None,
        "lookup_key": lookup_key,
        "active": True,
        "livemode": False,
        "type": "one_time",
        "billing_scheme": "per_unit",
        "custom_unit_amount": None,
        "product": {"id": "prod_test", "object": "product", "active": True, "livemode": False},
    }


def fixture_paths() -> tuple[Path, Path]:
    root = Path(__file__).parents[1] / "fixtures"
    return root / "catalogue.csv", root / "fulfilment-expansions.json"


def successful_transport() -> RecordingTransport:
    return RecordingTransport(
        [
            (200, {"data": [stripe_price("cr_test_template")]}),
            (200, {"data": [stripe_price("cr_test_bundle")]}),
            (200, {"data": [stripe_price("cr_test_free")]}),
            (200, {"data": [stripe_price("cr_test_support_hours")]}),
        ]
    )


def test_preflight_builds_the_same_public_manifest_deterministically():
    catalogue_path, expansions_path = fixture_paths()
    first_transport = successful_transport()
    first = asyncio.run(
        preflight_catalogue(
            catalogue_path=catalogue_path,
            fulfilment_expansions_path=expansions_path,
            environ={"STRIPE_API_KEY": "rk_test_preflight"},
            transport=first_transport,
        )
    )
    second = asyncio.run(
        preflight_catalogue(
            catalogue_path=catalogue_path,
            fulfilment_expansions_path=expansions_path,
            environ={"STRIPE_API_KEY": "rk_test_preflight"},
            transport=successful_transport(),
        )
    )

    assert first.version == second.version
    assert first.public_manifest() == second.public_manifest()
    encoded = json.dumps(preflight_output(first), sort_keys=True, separators=(",", ":"))
    assert encoded == json.dumps(preflight_output(second), sort_keys=True, separators=(",", ":"))
    assert "stripe_price_id" not in encoded
    assert "TEST-RESOURCE" not in encoded
    assert {request[0] for request in first_transport.requests} == {"GET"}
    assert {request[2]["Stripe-Version"] for request in first_transport.requests} == {"2025-09-30.clover"}
    assert all(request[3] is None for request in first_transport.requests)


def test_preflight_lock_preserves_private_test_price_resolutions(tmp_path):
    catalogue_path, expansions_path = fixture_paths()
    catalogue = asyncio.run(
        preflight_catalogue(
            catalogue_path=catalogue_path,
            fulfilment_expansions_path=expansions_path,
            environ={"STRIPE_API_KEY": "rk_test_preflight"},
            transport=successful_transport(),
        )
    )
    lock_path = tmp_path / "catalogue-preflight.lock.json"
    lock_path.write_text(json.dumps(preflight_lock_output(catalogue)))

    lock = PreflightLock.from_json(lock_path)

    assert lock.catalogue_version == catalogue.version
    assert asyncio.run(lock.resolve("cr_test_template")).stripe_price_id == "price_test_template"
    assert preflight_lock_output(catalogue)["stripe_mode"] == "test"


def test_preflight_lock_rejects_malformed_or_non_test_inputs(tmp_path):
    lock_path = tmp_path / "catalogue-preflight.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "api_version": "2025-09-30.clover",
                "catalogue_version": "sha256:" + "0" * 64,
                "prices": [],
                "schema": 1,
                "stripe_mode": "live",
            }
        )
    )

    with pytest.raises(CatalogueValidationError, match="preflight lock is invalid"):
        PreflightLock.from_json(lock_path)


def test_preflight_resolves_inactive_csv_rows_but_hides_them_from_public_output(tmp_path):
    catalogue_path, expansions_path = fixture_paths()
    inactive_csv = tmp_path / "catalogue.csv"
    inactive_csv.write_text(catalogue_path.read_text().replace("test-free-v1,1,true", "test-free-v1,1,false"))
    transport = successful_transport()

    catalogue = asyncio.run(
        preflight_catalogue(
            catalogue_path=inactive_csv,
            fulfilment_expansions_path=expansions_path,
            environ={"STRIPE_API_KEY": "rk_test_preflight"},
            transport=transport,
        )
    )

    assert len(transport.requests) == 4
    assert preflight_output(catalogue)["product_count"] == 4
    assert preflight_output(catalogue)["active_product_count"] == 3
    assert "TEST-FREE" not in {product["product_key"] for product in catalogue.public_manifest()["products"]}


@pytest.mark.parametrize(
    "environment", [{}, {"STRIPE_API_KEY": "sk_live_not_allowed"}, {"STRIPE_API_KEY": "pk_test_nope"}]
)
def test_preflight_rejects_missing_or_non_test_credentials_without_requests(environment):
    catalogue_path, expansions_path = fixture_paths()
    transport = successful_transport()

    with pytest.raises(CatalogueValidationError, match="test-mode API key"):
        asyncio.run(
            preflight_catalogue(
                catalogue_path=catalogue_path,
                fulfilment_expansions_path=expansions_path,
                environ=environment,
                transport=transport,
            )
        )

    assert transport.requests == []


def test_preflight_rejects_malformed_expansions_without_stripe_requests(tmp_path):
    catalogue_path, _ = fixture_paths()
    malformed_expansions = tmp_path / "expansions.json"
    malformed_expansions.write_text('{"test-template-v1": []}')
    transport = successful_transport()

    with pytest.raises(CatalogueValidationError, match="expansions are invalid"):
        asyncio.run(
            preflight_catalogue(
                catalogue_path=catalogue_path,
                fulfilment_expansions_path=malformed_expansions,
                environ={"STRIPE_API_KEY": "rk_test_preflight"},
                transport=transport,
            )
        )

    assert transport.requests == []


@pytest.mark.parametrize(
    "price",
    [
        {**stripe_price("cr_test_template"), "lookup_key": "cr_other"},
        {**stripe_price("cr_test_template"), "active": False},
        {**stripe_price("cr_test_template"), "livemode": True},
        {**stripe_price("cr_test_template"), "id": "not-a-price"},
        {**stripe_price("cr_test_template"), "object": "product"},
        {**stripe_price("cr_test_template"), "type": "recurring"},
        {**stripe_price("cr_test_template"), "billing_scheme": "tiered"},
        {**stripe_price("cr_test_template"), "custom_unit_amount": {"enabled": True}},
        {**stripe_price("cr_test_template"), "recurring": {"interval": "month"}},
        {**stripe_price("cr_test_template"), "product": "prod_unexpanded"},
        {
            **stripe_price("cr_test_template"),
            "product": {"id": "prod_test", "object": "product", "active": False, "livemode": False},
        },
        {
            **stripe_price("cr_test_template"),
            "product": {"id": "not-a-product", "object": "product", "active": True, "livemode": False},
        },
        {
            **stripe_price("cr_test_template"),
            "product": {"id": "prod_test", "object": "price", "active": True, "livemode": False},
        },
    ],
)
def test_preflight_price_resolver_fails_closed_for_non_test_product_or_price(price):
    resolver = StripeTestPreflightPriceResolver(
        StripeApiClient("rk_test_preflight", RecordingTransport([(200, {"data": [price]})]))
    )

    with pytest.raises(CatalogueValidationError):
        asyncio.run(resolver.resolve("cr_test_template"))


@pytest.mark.parametrize(
    "expansions",
    [
        '{"test-template-v1":["A"],"test-template-v1":["B"]}',
        '{" test-template-v1":["A"]}',
        '{"test-template-v1":[" A"]}',
    ],
)
def test_preflight_rejects_ambiguous_expansions_without_stripe_requests(tmp_path, expansions):
    catalogue_path, _ = fixture_paths()
    expansions_path = tmp_path / "expansions.json"
    expansions_path.write_text(expansions)
    transport = successful_transport()

    with pytest.raises(CatalogueValidationError, match="expansions"):
        asyncio.run(
            preflight_catalogue(
                catalogue_path=catalogue_path,
                fulfilment_expansions_path=expansions_path,
                environ={"STRIPE_API_KEY": "rk_test_preflight"},
                transport=transport,
            )
        )

    assert transport.requests == []


def test_preflight_transport_rejects_redirects_before_the_authorization_header_can_be_forwarded():
    request = Request("https://api.stripe.com/v1/prices", headers={"Authorization": "Basic secret"})

    assert (
        preflight._RejectRedirects().redirect_request(
            request, BytesIO(), 302, "redirect", {}, "https://attacker.invalid/collect"
        )
        is None
    )


def test_preflight_transport_caps_response_bodies():
    assert preflight._read_response(BytesIO(b"x" * (preflight._MAX_RESPONSE_BYTES + 1))) == b""


def test_preflight_cli_returns_a_sanitized_error_without_echoing_credentials(monkeypatch, capsys):
    catalogue_path, expansions_path = fixture_paths()
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "preflight_catalogue.py"))
    secret = "sk_live_this_must_not_be_printed"
    monkeypatch.setenv("STRIPE_API_KEY", secret)

    assert module["main"](["--catalogue", str(catalogue_path), "--fulfilment-expansions", str(expansions_path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "catalogue preflight failed\n"
    assert secret not in captured.err
