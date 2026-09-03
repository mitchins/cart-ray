from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalogue import Catalogue, CsvCatalogueSourceAdapter, PriceResolver, build_catalogue_from_source
from .errors import CatalogueValidationError
from .models import ResolvedPrice
from .stripe import STRIPE_API_VERSION, AsyncStripeTransport, StripeApiClient

_STRIPE_API_BASE_URL = "https://api.stripe.com"
_STRIPE_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 1_000_000


class _RejectRedirects(HTTPRedirectHandler):
    """Fails closed so Authorization can never cross an HTTP redirect."""

    def redirect_request(self, _request, _fp, _code, _message, _headers, _new_url):
        return None


_STRIPE_OPENER = build_opener(_RejectRedirects())


class UrllibStripeTransport:
    """Makes bounded, read-only Stripe API requests for an operator preflight."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: str | None = None,
    ) -> tuple[int, Mapping[str, object]]:
        if method != "GET" or body is not None or not path.startswith("/v1/"):
            raise CatalogueValidationError("catalogue preflight permits only Stripe GET requests")
        return await asyncio.to_thread(self._request, path, headers)

    @staticmethod
    def _request(path: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, object]]:
        request = Request(f"{_STRIPE_API_BASE_URL}{path}", headers=dict(headers), method="GET")
        try:
            with _STRIPE_OPENER.open(request, timeout=_STRIPE_TIMEOUT_SECONDS) as response:
                return response.status, _decode_json(_read_response(response))
        except HTTPError as error:
            return error.code, _decode_json(_read_response(error))


def _read_response(response: Any) -> bytes:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    return raw if len(raw) <= _MAX_RESPONSE_BYTES else b""


def _decode_json(raw: bytes) -> Mapping[str, object]:
    try:
        decoded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "Stripe response was not valid JSON"}
    return decoded if isinstance(decoded, dict) else {"error": "Stripe response was not an object"}


def require_test_stripe_api_key(environ: Mapping[str, str]) -> str:
    """Reads a test-only server credential without accepting CLI-supplied keys."""

    key = environ.get("STRIPE_API_KEY")
    if not isinstance(key, str) or not key.startswith(("rk_test_", "sk_test_")):
        raise CatalogueValidationError("catalogue preflight requires a Stripe test-mode API key")
    return key


class StripeTestPreflightPriceResolver:
    """Resolves only active, fixed, test-mode Stripe Prices and Products for preflight."""

    def __init__(self, client: StripeApiClient) -> None:
        self.client = client

    async def resolve(self, lookup_key: str) -> ResolvedPrice:
        payload = await self.client.get(
            "/v1/prices",
            {
                "active": "true",
                "type": "one_time",
                "lookup_keys[]": lookup_key,
                "limit": "2",
                "expand[]": "data.product",
            },
        )
        prices = payload.get("data")
        if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], dict):
            raise CatalogueValidationError(f"expected exactly one active Stripe Price for lookup key: {lookup_key}")
        price = prices[0]
        try:
            price_id = price["id"]
            price_object = price["object"]
            amount = price["unit_amount"]
            currency = price["currency"]
            recurring = price["recurring"]
            price_lookup_key = price["lookup_key"]
            price_active = price["active"]
            price_livemode = price["livemode"]
            price_type = price["type"]
            billing_scheme = price["billing_scheme"]
            custom_unit_amount = price["custom_unit_amount"]
            product = price["product"]
        except KeyError as error:
            raise CatalogueValidationError(f"Stripe Price is incomplete for lookup key: {lookup_key}") from error
        if not isinstance(product, dict):
            raise CatalogueValidationError(f"Stripe Product is incomplete for lookup key: {lookup_key}")
        try:
            product_id = product["id"]
            product_object = product["object"]
            product_active = product["active"]
            product_livemode = product["livemode"]
        except KeyError as error:
            raise CatalogueValidationError(f"Stripe Product is incomplete for lookup key: {lookup_key}") from error
        if (
            not isinstance(price_id, str)
            or not price_id.startswith("price_")
            or price_object != "price"
            or not isinstance(amount, int)
            or isinstance(amount, bool)
            or not isinstance(currency, str)
            or price_lookup_key != lookup_key
            or price_active is not True
            or price_livemode is not False
            or price_type != "one_time"
            or billing_scheme != "per_unit"
            or custom_unit_amount is not None
            or recurring is not None
            or not isinstance(product_id, str)
            or not product_id.startswith("prod_")
            or product_object != "product"
            or product_active is not True
            or product_livemode is not False
            or amount < 0
            or len(currency) != 3
        ):
            raise CatalogueValidationError(f"Stripe Price is invalid for lookup key: {lookup_key}")
        return ResolvedPrice(stripe_price_id=price_id, amount_minor=amount, currency=currency)


@dataclass(frozen=True)
class PreflightLock:
    """Private, reviewed test-mode price resolution used to compile a real subset."""

    catalogue_version: str
    prices: Mapping[str, ResolvedPrice]

    @classmethod
    def from_json(cls, path: Path) -> PreflightLock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CatalogueValidationError("preflight lock is unavailable") from error
        expected_keys = {"api_version", "catalogue_version", "prices", "schema", "stripe_mode"}
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise CatalogueValidationError("preflight lock is invalid")
        if raw["schema"] != 1 or raw["stripe_mode"] != "test" or raw["api_version"] != STRIPE_API_VERSION:
            raise CatalogueValidationError("preflight lock is invalid")
        catalogue_version = raw["catalogue_version"]
        rows = raw["prices"]
        if (
            not isinstance(catalogue_version, str)
            or len(catalogue_version) != 71
            or not catalogue_version.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in catalogue_version.removeprefix("sha256:"))
            or not isinstance(rows, list)
        ):
            raise CatalogueValidationError("preflight lock is invalid")
        prices: dict[str, ResolvedPrice] = {}
        for row in rows:
            expected_row_keys = {"amount_minor", "currency", "stripe_lookup_key", "stripe_price_id"}
            if not isinstance(row, dict) or set(row) != expected_row_keys:
                raise CatalogueValidationError("preflight lock is invalid")
            lookup_key = row["stripe_lookup_key"]
            price_id = row["stripe_price_id"]
            amount = row["amount_minor"]
            currency = row["currency"]
            if (
                not isinstance(lookup_key, str)
                or not lookup_key
                or lookup_key != lookup_key.strip()
                or lookup_key in prices
                or not isinstance(price_id, str)
                or not price_id.startswith("price_")
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount < 0
                or not isinstance(currency, str)
                or len(currency) != 3
            ):
                raise CatalogueValidationError("preflight lock is invalid")
            prices[lookup_key] = ResolvedPrice(stripe_price_id=price_id, amount_minor=amount, currency=currency)
        if not prices:
            raise CatalogueValidationError("preflight lock is invalid")
        return cls(catalogue_version=catalogue_version, prices=prices)

    async def resolve(self, lookup_key: str) -> ResolvedPrice:
        try:
            return self.prices[lookup_key]
        except KeyError as error:
            raise CatalogueValidationError(f"preflight lock does not contain lookup key: {lookup_key}") from error


def load_fulfilment_expansions(path: Path) -> dict[str, tuple[str, ...]]:
    """Loads the private local expansion input without exposing it in preflight output."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CatalogueValidationError("fulfilment expansions are unavailable") from error
    if not isinstance(raw, dict) or not raw:
        raise CatalogueValidationError("fulfilment expansions are invalid")
    expansions: dict[str, tuple[str, ...]] = {}
    for version, resources in raw.items():
        if (
            not isinstance(version, str)
            or not version
            or version != version.strip()
            or not isinstance(resources, list)
            or not resources
            or any(
                not isinstance(resource, str) or not resource or resource != resource.strip() for resource in resources
            )
            or len(resources) != len(set(resources))
        ):
            raise CatalogueValidationError("fulfilment expansions are invalid")
        expansions[version] = tuple(resources)
    return expansions


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def preflight_catalogue(
    *,
    catalogue_path: Path,
    fulfilment_expansions_path: Path,
    environ: Mapping[str, str],
    transport: AsyncStripeTransport,
) -> Catalogue:
    """Builds a catalogue from explicit local files after read-only Stripe test validation."""

    api_key = require_test_stripe_api_key(environ)
    expansions = load_fulfilment_expansions(fulfilment_expansions_path)
    resolver: PriceResolver = StripeTestPreflightPriceResolver(StripeApiClient(api_key, transport))
    return await build_catalogue_from_source(CsvCatalogueSourceAdapter(catalogue_path), resolver, expansions)


def preflight_output(catalogue: Catalogue) -> dict[str, object]:
    """Returns the stable, non-sensitive result shape for successful operator runs."""

    public_manifest = catalogue.public_manifest()
    return {
        "api_version": STRIPE_API_VERSION,
        "catalogue": public_manifest,
        "active_product_count": len(public_manifest["products"]),
        "product_count": len(catalogue.products),
        "status": "ok",
        "stripe_mode": "test",
    }


def preflight_lock_output(catalogue: Catalogue) -> dict[str, object]:
    """Produces the private checked-in input that binds compilation to a Stripe test preflight."""

    return {
        "api_version": STRIPE_API_VERSION,
        "catalogue_version": catalogue.version,
        "prices": [
            {
                "amount_minor": product.price.amount_minor,
                "currency": product.price.currency,
                "stripe_lookup_key": product.source.stripe_lookup_key,
                "stripe_price_id": product.price.stripe_price_id,
            }
            for _, product in sorted(catalogue.products.items())
        ],
        "schema": 1,
        "stripe_mode": "test",
    }
