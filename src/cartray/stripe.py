from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from .errors import CatalogueValidationError
from .models import CheckoutRedirect, CheckoutSpec, ResolvedPrice


class StripeApiError(RuntimeError):
    """A Stripe response could not be used to create a trusted checkout."""


class ProjectionSealError(RuntimeError):
    """A server-authored CartRay projection could not be sealed safely."""


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
        if not session_id.startswith("cs_"):
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
        request_headers = {"Authorization": f"Basic {authorization}"}
        request_headers.update(headers or {})
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
