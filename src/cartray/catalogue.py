from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .canonical import PRODUCT_KEY_RE
from .errors import CatalogueValidationError, CheckoutValidationError
from .models import CatalogProduct, CatalogSource, ResolvedPrice


class PriceResolver(Protocol):
    async def resolve(self, lookup_key: str) -> ResolvedPrice: ...


@dataclass(frozen=True)
class FixturePriceResolver:
    prices: Mapping[str, ResolvedPrice]

    @classmethod
    def from_json(cls, path: Path) -> FixturePriceResolver:
        raw = json.loads(path.read_text())
        return cls({key: ResolvedPrice(**value) for key, value in raw.items()})

    async def resolve(self, lookup_key: str) -> ResolvedPrice:
        try:
            return self.prices[lookup_key]
        except KeyError as error:
            raise CatalogueValidationError(f"unknown Stripe lookup key: {lookup_key}") from error


@dataclass(frozen=True)
class Catalogue:
    version: str
    products: Mapping[str, CatalogProduct]

    def product(self, product_key: str) -> CatalogProduct:
        try:
            product = self.products[product_key]
        except KeyError as error:
            raise CheckoutValidationError(f"unknown product key: {product_key}") from error
        if not product.source.active:
            raise CheckoutValidationError(f"inactive product key: {product_key}")
        return product

    def public_manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "products": [
                {
                    "product_key": key,
                    "title": product.source.title,
                    "amount_minor": product.price.amount_minor,
                    "currency": product.price.currency,
                    "max_quantity": product.source.max_quantity,
                }
                for key, product in sorted(self.products.items())
                if product.source.active
            ],
        }

    def private_manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "products": {
                key: {
                    "stripe_price_id": product.price.stripe_price_id,
                    "fulfilment_type": product.source.fulfilment_type,
                    "fulfilment_version": product.source.fulfilment_version,
                    "fulfilment_resources": product.fulfilment_resources,
                }
                for key, product in self.products.items()
            },
        }


def load_catalogue_sources(path: Path) -> tuple[CatalogSource, ...]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        rows = list(reader)
    expected = {
        "product_key",
        "title",
        "stripe_lookup_key",
        "fulfilment_type",
        "fulfilment_version",
        "max_quantity",
        "active",
    }
    if len(headers) != len(expected) or set(headers) != expected:
        raise CatalogueValidationError("catalogue CSV headers do not match the contract")
    if not rows:
        raise CatalogueValidationError("catalogue CSV must contain at least one product")

    sources: list[CatalogSource] = []
    for row in rows:
        if None in row or any(not isinstance(row.get(header), str) or not row[header].strip() for header in headers):
            raise CatalogueValidationError(f"invalid row: {row!r}")
        try:
            active = {"true": True, "false": False}[row["active"].lower()]
            source = CatalogSource(
                product_key=row["product_key"],
                title=row["title"],
                stripe_lookup_key=row["stripe_lookup_key"],
                fulfilment_type=row["fulfilment_type"],
                fulfilment_version=row["fulfilment_version"],
                max_quantity=int(row["max_quantity"]),
                active=active,
            )
        except (KeyError, ValueError) as error:
            raise CatalogueValidationError(f"invalid row: {row!r}") from error
        if not PRODUCT_KEY_RE.fullmatch(source.product_key):
            raise CatalogueValidationError(f"invalid product key: {source.product_key!r}")
        if not source.title or not source.stripe_lookup_key or source.max_quantity < 1:
            raise CatalogueValidationError(f"incomplete product: {source.product_key}")
        sources.append(source)

    keys = [source.product_key for source in sources]
    if len(keys) != len(set(keys)):
        raise CatalogueValidationError("duplicate product keys")
    return tuple(sources)


async def build_catalogue(
    sources: tuple[CatalogSource, ...],
    price_resolver: PriceResolver,
    fulfilment_expansions: Mapping[str, tuple[str, ...]],
) -> Catalogue:
    products: dict[str, CatalogProduct] = {}
    for source in sources:
        price = await price_resolver.resolve(source.stripe_lookup_key)
        if not price.stripe_price_id or price.amount_minor < 0 or len(price.currency) != 3:
            raise CatalogueValidationError(f"invalid resolved price: {source.product_key}")
        try:
            resources = fulfilment_expansions[source.fulfilment_version]
        except KeyError as error:
            raise CatalogueValidationError(f"missing fulfilment expansion: {source.fulfilment_version}") from error
        if not resources or len(resources) != len(set(resources)):
            raise CatalogueValidationError(f"invalid fulfilment expansion: {source.product_key}")
        products[source.product_key] = CatalogProduct(source, price, tuple(resources))

    version_payload = {
        key: {
            "source": product.source.__dict__,
            "price": product.price.__dict__,
            "resources": product.fulfilment_resources,
        }
        for key, product in sorted(products.items())
    }
    encoded = json.dumps(version_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Catalogue("sha256:" + sha256(encoded.encode("utf-8")).hexdigest(), products)
