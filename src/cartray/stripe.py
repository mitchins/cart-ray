from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from .canonical import MAX_METADATA_CHUNKS, CanonicalItem, build_item_chunks, parse_projection_items
from .errors import CatalogueValidationError, CheckoutValidationError
from .models import CheckoutRedirect, CheckoutSpec, ResolvedPrice


class StripeApiError(RuntimeError):
    """A Stripe response could not be used to create a trusted checkout."""


class ProjectionSealError(RuntimeError):
    """A server-authored CartRay projection could not be sealed safely."""


MAX_STRIPE_LINE_ITEMS = 100
MAX_STRIPE_LINE_ITEM_PAGES = 10
STRIPE_API_VERSION = "2023-08-16"
_STRIPE_SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")


class AsyncStripeTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: str | None = None,
    ) -> tuple[int, Mapping[str, object]]: ...


class ProjectionSigner(Protocol):
    async def sign(self, payload: bytes) -> bytes: ...


class ProjectionVerifier(Protocol):
    async def verify(self, payload: bytes, signature: bytes) -> bool: ...


def signature_payload(*, session_id: str, environment: str, metadata: Mapping[str, str]) -> bytes:
    """The exact CartRay projection an Ed25519 signer authenticates."""

    required = {
        "cr_schema",
        "cr_source",
        "cr_order_id",
        "cr_catalogue_version",
        "cr_item_count",
        "cr_items_digest",
        "cr_nonce",
        "cr_kid",
    }
    if set(metadata) & {"cr_signature"}:
        raise ProjectionSealError("the unsigned projection must not contain a signature")
    missing = required - set(metadata)
    if missing:
        raise ProjectionSealError(f"the projection cannot be signed without: {sorted(missing)!r}")
    payload = {
        "catalogue_version": metadata["cr_catalogue_version"],
        "environment": environment,
        "item_count": metadata["cr_item_count"],
        "items_digest": metadata["cr_items_digest"],
        "key_id": metadata["cr_kid"],
        "nonce": metadata["cr_nonce"],
        "order_id": metadata["cr_order_id"],
        "schema": metadata["cr_schema"],
        "session_id": session_id,
        "source": metadata["cr_source"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


@dataclass(frozen=True)
class CheckoutMetadataSealer:
    environment: str
    key_id: str
    signer: ProjectionSigner

    async def seal(self, *, session_id: str, metadata: Mapping[str, str]) -> dict[str, str]:
        if not _STRIPE_SESSION_ID_RE.fullmatch(session_id):
            raise ProjectionSealError("Stripe returned an invalid Checkout Session ID")
        unsigned = dict(metadata)
        if "cr_kid" in unsigned and unsigned["cr_kid"] != self.key_id:
            raise ProjectionSealError("the projection key ID does not match the active signing key")
        unsigned["cr_kid"] = self.key_id
        payload = signature_payload(session_id=session_id, environment=self.environment, metadata=unsigned)
        signature = await self.signer.sign(payload)
        if not signature:
            raise ProjectionSealError("the CartRay signing key returned an empty signature")
        unsigned["cr_signature"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return unsigned


@dataclass(frozen=True)
class CheckoutMetadataVerifier:
    environment: str
    verifiers: Mapping[str, ProjectionVerifier]

    async def verify(self, *, session_id: str, metadata: Mapping[str, str]) -> tuple[CanonicalItem, ...]:
        required = {
            "cr_schema",
            "cr_source",
            "cr_order_id",
            "cr_catalogue_version",
            "cr_item_count",
            "cr_chunk_count",
            "cr_items_digest",
            "cr_nonce",
            "cr_kid",
            "cr_signature",
        }
        if metadata.get("cr_schema") != "1" or metadata.get("cr_source") != "cartray":
            raise ProjectionSealError("Stripe Session has an unsupported CartRay projection")
        allowed_keys = required | {f"cr_items_{index:02d}" for index in range(1, MAX_METADATA_CHUNKS + 1)}
        if any(key.startswith("cr_") and key not in allowed_keys for key in metadata):
            raise ProjectionSealError("Stripe Session has unknown CartRay projection fields")
        if not required <= set(metadata):
            raise ProjectionSealError("Stripe Session has an incomplete CartRay projection")
        try:
            verifier = self.verifiers[metadata["cr_kid"]]
            signature = _base64url_decode(metadata["cr_signature"])
        except (KeyError, ValueError, TypeError) as error:
            raise ProjectionSealError("Stripe Session has an untrusted CartRay projection key") from error
        unsigned = {key: value for key, value in metadata.items() if key != "cr_signature"}
        try:
            items = parse_projection_items(unsigned)
            payload = signature_payload(session_id=session_id, environment=self.environment, metadata=unsigned)
        except (CheckoutValidationError, ProjectionSealError) as error:
            raise ProjectionSealError("Stripe Session has an invalid CartRay projection") from error
        expected_chunks = build_item_chunks(items)
        if (
            unsigned["cr_item_count"] != str(len(items))
            or unsigned["cr_chunk_count"] != str(len(expected_chunks))
            or any(unsigned[f"cr_items_{index:02d}"] != chunk for index, chunk in enumerate(expected_chunks, start=1))
        ):
            raise ProjectionSealError("Stripe Session has a non-canonical CartRay projection")
        if not await verifier.verify(payload, signature):
            raise ProjectionSealError("Stripe Session has an invalid CartRay projection signature")
        return items


def _base64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class StripeApiClient:
    secret_key: str
    transport: AsyncStripeTransport

    async def get(self, path: str, query: Mapping[str, str]) -> Mapping[str, object]:
        encoded = urlencode(query)
        return await self._request("GET", f"{path}?{encoded}" if encoded else path)

    async def post(
        self,
        path: str,
        form: Mapping[str, str],
        *,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await self._request("POST", path, headers=headers, body=urlencode(form))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str | None = None,
    ) -> Mapping[str, object]:
        if not self.secret_key.startswith(("rk_test_", "sk_test_")):
            raise StripeApiError("CartRay accepts only Stripe test-mode keys in this milestone")
        authorization = base64.b64encode(f"{self.secret_key}:".encode()).decode("ascii")
        request_headers = {
            "Authorization": f"Basic {authorization}",
        }
        request_headers.update(headers or {})
        request_headers["Stripe-Version"] = STRIPE_API_VERSION
        status, payload = await self.transport.request(method, path, headers=request_headers, body=body)
        if not 200 <= status < 300:
            message = payload.get("error", "Stripe request failed")
            raise StripeApiError(f"Stripe API {method} {path} failed: {message!r}")
        return payload


@dataclass(frozen=True)
class StripePriceResolver:
    client: StripeApiClient

    async def resolve(self, lookup_key: str) -> ResolvedPrice:
        payload = await self.client.get("/v1/prices", {"active": "true", "lookup_keys[]": lookup_key, "limit": "2"})
        prices = payload.get("data")
        if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], dict):
            raise CatalogueValidationError(f"expected exactly one active Stripe Price for lookup key: {lookup_key}")
        price = prices[0]
        try:
            price_id = price["id"]
            amount = price["unit_amount"]
            currency = price["currency"]
            recurring = price["recurring"]
        except KeyError as error:
            raise CatalogueValidationError(f"Stripe Price is incomplete for lookup key: {lookup_key}") from error
        if (
            not isinstance(price_id, str)
            or not isinstance(amount, int)
            or not isinstance(currency, str)
            or recurring is not None
            or amount < 0
            or len(currency) != 3
        ):
            raise CatalogueValidationError(f"Stripe Price is invalid for lookup key: {lookup_key}")
        return ResolvedPrice(stripe_price_id=price_id, amount_minor=amount, currency=currency)


@dataclass(frozen=True)
class StripeLineItem:
    line_item_id: str
    price_id: str
    quantity: int
    unit_amount_minor: int
    currency: str


@dataclass(frozen=True)
class StripeCheckoutSession:
    session_id: str
    livemode: bool
    mode: str
    status: str
    payment_status: str
    amount_total_minor: int
    currency: str
    metadata: Mapping[str, str]
    line_items: tuple[StripeLineItem, ...]


@dataclass(frozen=True)
class StripeCheckoutSessionRetriever:
    client: StripeApiClient

    async def retrieve(self, session_id: str) -> StripeCheckoutSession:
        if not _STRIPE_SESSION_ID_RE.fullmatch(session_id):
            raise StripeApiError("Stripe webhook identified an invalid Checkout Session ID")
        session = await self.client.get(f"/v1/checkout/sessions/{session_id}", {})
        if session.get("id") != session_id:
            raise StripeApiError("Stripe returned a different Checkout Session")
        line_items = await self._line_items(session_id)
        try:
            metadata = session["metadata"]
            livemode = session["livemode"]
            mode = session["mode"]
            status = session["status"]
            payment_status = session["payment_status"]
            amount_total_minor = session["amount_total"]
            currency = session["currency"]
        except KeyError as error:
            raise StripeApiError("Stripe Checkout Session is incomplete") from error
        if (
            not isinstance(metadata, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items())
            or not isinstance(livemode, bool)
            or not isinstance(mode, str)
            or not isinstance(status, str)
            or not isinstance(payment_status, str)
            or not isinstance(amount_total_minor, int)
            or isinstance(amount_total_minor, bool)
            or not isinstance(currency, str)
            or amount_total_minor < 0
            or len(currency) != 3
        ):
            raise StripeApiError("Stripe Checkout Session is invalid")
        return StripeCheckoutSession(
            session_id,
            livemode,
            mode,
            status,
            payment_status,
            amount_total_minor,
            currency,
            metadata,
            line_items,
        )

    async def _line_items(self, session_id: str) -> tuple[StripeLineItem, ...]:
        line_items: list[StripeLineItem] = []
        line_item_ids: set[str] = set()
        cursors: set[str] = set()
        starting_after: str | None = None
        for _page in range(MAX_STRIPE_LINE_ITEM_PAGES):
            query = {"limit": "100"}
            if starting_after is not None:
                query["starting_after"] = starting_after
            page = await self.client.get(f"/v1/checkout/sessions/{session_id}/line_items", query)
            raw_items = page.get("data")
            has_more = page.get("has_more")
            if not isinstance(raw_items, list) or not isinstance(has_more, bool) or not raw_items:
                raise StripeApiError("Stripe Checkout Session line items are invalid")
            page_items = tuple(_line_item(item) for item in raw_items)
            page_line_item_ids = {item.line_item_id for item in page_items}
            if len(page_line_item_ids) != len(page_items) or page_line_item_ids & line_item_ids:
                raise StripeApiError("Stripe Checkout Session line items contain duplicates")
            line_item_ids.update(page_line_item_ids)
            line_items.extend(page_items)
            if len(line_items) > MAX_STRIPE_LINE_ITEMS:
                raise StripeApiError("Stripe Checkout Session has too many line items")
            if not has_more:
                return tuple(line_items)
            starting_after = page_items[-1].line_item_id
            if starting_after in cursors:
                raise StripeApiError("Stripe Checkout Session line-item pagination repeated a cursor")
            cursors.add(starting_after)
        raise StripeApiError("Stripe Checkout Session line-item pagination exceeded its limit")


def _line_item(payload: object) -> StripeLineItem:
    if not isinstance(payload, dict):
        raise StripeApiError("Stripe Checkout Session line item is invalid")
    try:
        line_item_id = payload["id"]
        price = payload["price"]
        quantity = payload["quantity"]
    except KeyError as error:
        raise StripeApiError("Stripe Checkout Session line item is incomplete") from error
    if not isinstance(price, dict):
        raise StripeApiError("Stripe Checkout Session line item price is invalid")
    try:
        price_id = price["id"]
        unit_amount_minor = price["unit_amount"]
        currency = price["currency"]
    except KeyError as error:
        raise StripeApiError("Stripe Checkout Session line item price is incomplete") from error
    if (
        not isinstance(line_item_id, str)
        or not line_item_id
        or not isinstance(price_id, str)
        or not price_id.startswith("price_")
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity < 1
        or not isinstance(unit_amount_minor, int)
        or isinstance(unit_amount_minor, bool)
        or unit_amount_minor < 0
        or not isinstance(currency, str)
        or len(currency) != 3
    ):
        raise StripeApiError("Stripe Checkout Session line item is invalid")
    return StripeLineItem(line_item_id, price_id, quantity, unit_amount_minor, currency)


@dataclass(frozen=True)
class StripeCheckoutGateway:
    client: StripeApiClient
    sealer: CheckoutMetadataSealer

    async def create_checkout(self, spec: CheckoutSpec) -> CheckoutRedirect:
        session = await self.client.post(
            "/v1/checkout/sessions",
            _checkout_session_form(spec),
            idempotency_key=spec.idempotency_key,
        )
        session_id = session.get("id")
        url = session.get("url")
        if not isinstance(session_id, str) or not isinstance(url, str):
            raise StripeApiError("Stripe did not return a Checkout Session URL")

        sealed_metadata = await self.sealer.seal(session_id=session_id, metadata=spec.metadata)
        metadata_form = {f"metadata[{key}]": value for key, value in sealed_metadata.items()}
        await self.client.post(f"/v1/checkout/sessions/{session_id}", metadata_form)

        payment_intent = session.get("payment_intent")
        if payment_intent is not None:
            if not isinstance(payment_intent, str) or not payment_intent.startswith("pi_"):
                raise StripeApiError("Stripe returned an invalid PaymentIntent ID")
            await self.client.post(
                f"/v1/payment_intents/{payment_intent}",
                metadata_form,
            )
        return CheckoutRedirect(session_id=session_id, url=url)


def _checkout_session_form(spec: CheckoutSpec) -> dict[str, str]:
    form = {
        "mode": "payment",
        "success_url": spec.success_url,
        "cancel_url": spec.cancel_url,
        "allow_promotion_codes": "true",
    }
    for index, (price_id, quantity) in enumerate(spec.line_items):
        form[f"line_items[{index}][price]"] = price_id
        form[f"line_items[{index}][quantity]"] = str(quantity)
    return form
