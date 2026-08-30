from __future__ import annotations

from dataclasses import dataclass

from .canonical import CanonicalItem
from .errors import CheckoutValidationError


@dataclass(frozen=True)
class CatalogSource:
    product_key: str
    title: str
    stripe_lookup_key: str
    fulfilment_type: str
    fulfilment_version: str
    max_quantity: int
    active: bool


@dataclass(frozen=True)
class ResolvedPrice:
    stripe_price_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class CatalogProduct:
    source: CatalogSource
    price: ResolvedPrice
    fulfilment_resources: tuple[str, ...]


@dataclass(frozen=True)
class CheckoutRequest:
    checkout_request_id: str
    manifest_version: str
    items: tuple[CanonicalItem, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> CheckoutRequest:
        allowed = {"checkout_request_id", "manifest_version", "items"}
        unknown = set(payload) - allowed
        if unknown:
            raise CheckoutValidationError(f"unsupported checkout fields: {sorted(unknown)!r}")
        try:
            raw_items = payload["items"]
            if not isinstance(raw_items, list):
                raise TypeError
            items = tuple(
                CanonicalItem(str(item["product_key"]), item["quantity"])
                for item in raw_items
                if isinstance(item, dict)
            )
            if len(items) != len(raw_items):
                raise TypeError
            return cls(
                checkout_request_id=str(payload["checkout_request_id"]),
                manifest_version=str(payload["manifest_version"]),
                items=items,
            )
        except (KeyError, TypeError) as error:
            raise CheckoutValidationError("invalid checkout request") from error


@dataclass(frozen=True)
class OrderItem:
    product_key: str
    quantity: int
    stripe_price_id: str
    unit_amount_minor: int
    fulfilment_resources: tuple[str, ...]


@dataclass(frozen=True)
class CheckoutOrder:
    order_id: str
    checkout_request_id: str
    request_fingerprint: str
    manifest_version: str
    items: tuple[OrderItem, ...]
    items_digest: str
    currency: str
    subtotal_minor: int


@dataclass(frozen=True)
class CheckoutSpec:
    order_id: str
    idempotency_key: str
    line_items: tuple[tuple[str, int], ...]
    success_url: str
    cancel_url: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class CheckoutRedirect:
    session_id: str
    url: str
