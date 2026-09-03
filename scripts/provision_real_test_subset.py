from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cartray.errors import CatalogueValidationError
from cartray.stripe import StripeApiClient, StripeApiError

_STRIPE_API_BASE_URL = "https://api.stripe.com"
_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 1_000_000


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, _request, _fp, _code, _message, _headers, _new_url):
        return None


_OPENER = build_opener(_RejectRedirects())


@dataclass(frozen=True)
class ProductSpec:
    product_key: str
    title: str
    lookup_key: str
    amount_minor: int


SPECS = (
    ProductSpec(
        product_key="EP-SIL-2026",
        title="Module 5a - Supported Independent Living Policy and Procedure Package",
        lookup_key="cr_test_ep_sil_2026",
        amount_minor=85000,
    ),
    ProductSpec(
        product_key="EP-LMS-TRAINING-CATALOGUE",
        title="LMS Training Catalogue",
        lookup_key="cr_test_ep_lms_training_catalogue",
        amount_minor=0,
    ),
)


class UrllibStripeWriteTransport:
    """Makes bounded Stripe API calls for the explicitly approved test subset only."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: str | None = None,
    ) -> tuple[int, Mapping[str, object]]:
        return await asyncio.to_thread(self._request, method, path, headers, body)

    @staticmethod
    def _request(
        method: str, path: str, headers: Mapping[str, str], body: str | None
    ) -> tuple[int, Mapping[str, object]]:
        request = Request(
            f"{_STRIPE_API_BASE_URL}{path}",
            data=body.encode("utf-8") if body is not None else None,
            headers=dict(headers),
            method=method,
        )
        try:
            with _OPENER.open(request, timeout=_TIMEOUT_SECONDS) as response:
                return response.status, _decode_json(_read_response(response))
        except HTTPError as error:
            return error.code, _decode_json(_read_response(error))
        except URLError as error:
            raise StripeApiError("Stripe test provisioning request failed") from error


def _read_response(response: Any) -> bytes:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    return raw if len(raw) <= _MAX_RESPONSE_BYTES else b""


def _decode_json(raw: bytes) -> Mapping[str, object]:
    try:
        decoded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "Stripe response was not valid JSON"}
    return decoded if isinstance(decoded, dict) else {"error": "Stripe response was not an object"}


def require_test_secret_key(environ: Mapping[str, str]) -> str:
    key = environ.get("STRIPE_API_KEY")
    if not isinstance(key, str) or not key.startswith(("rk_test_", "sk_test_")):
        raise CatalogueValidationError("Stripe test provisioning requires an rk_test_ or sk_test_ API key")
    return key


def _valid_existing_price(price: Mapping[str, object], spec: ProductSpec) -> bool:
    return (
        isinstance(price.get("id"), str)
        and price["id"].startswith("price_")
        and price.get("object") == "price"
        and price.get("active") is True
        and price.get("livemode") is False
        and price.get("type") == "one_time"
        and price.get("billing_scheme") == "per_unit"
        and price.get("custom_unit_amount") is None
        and price.get("recurring") is None
        and price.get("currency") == "aud"
        and price.get("unit_amount") == spec.amount_minor
        and price.get("lookup_key") == spec.lookup_key
    )


async def provision(client: StripeApiClient, spec: ProductSpec) -> dict[str, str]:
    existing = await client.get("/v1/prices", {"active": "true", "lookup_keys[]": spec.lookup_key, "limit": "2"})
    prices = existing.get("data")
    if not isinstance(prices, list) or any(not isinstance(price, dict) for price in prices):
        raise StripeApiError("Stripe test provisioning returned an invalid price list")
    if len(prices) == 1:
        price = prices[0]
        if not _valid_existing_price(price, spec):
            raise StripeApiError(f"existing test Price is incompatible for {spec.product_key}")
        return {"product_key": spec.product_key, "price_id": price["id"], "status": "existing"}
    if len(prices) > 1:
        raise StripeApiError(f"multiple active test Prices exist for {spec.product_key}")

    price = await client.post(
        "/v1/prices",
        {
            "active": "true",
            "currency": "aud",
            "lookup_key": spec.lookup_key,
            "metadata[cr_product_key]": spec.product_key,
            "product_data[active]": "true",
            "product_data[metadata][cr_product_key]": spec.product_key,
            "product_data[name]": spec.title,
            "unit_amount": str(spec.amount_minor),
        },
        idempotency_key=f"cartray-m8c-test-price:{spec.product_key}",
    )
    if not _valid_existing_price(price, spec):
        raise StripeApiError(f"Stripe returned an incompatible test Price for {spec.product_key}")
    return {"product_key": spec.product_key, "price_id": price["id"], "status": "created"}


async def run() -> list[dict[str, str]]:
    client = StripeApiClient(require_test_secret_key(os.environ), UrllibStripeWriteTransport())
    return [await provision(client, spec) for spec in SPECS]


def main() -> int:
    try:
        result = asyncio.run(run())
    except (CatalogueValidationError, StripeApiError, OSError, ValueError):
        print("Stripe test provisioning failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
