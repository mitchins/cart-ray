from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .canonical import (
    CanonicalItem,
    canonical_items,
    items_digest,
    projection_metadata,
    request_fingerprint,
)
from .catalogue import Catalogue
from .errors import CheckoutValidationError
from .gateway import PaymentGateway
from .models import CheckoutOrder, CheckoutRedirect, CheckoutRequest, CheckoutSpec, OrderItem
from .store import SqliteOrderStore


@dataclass
class CheckoutService:
    catalogue: Catalogue
    store: SqliteOrderStore
    gateway: PaymentGateway
    success_url: str = "https://store.invalid/purchase-complete/"
    cancel_url: str = "https://store.invalid/checkout-cancelled/"

    def checkout(self, request: CheckoutRequest) -> CheckoutRedirect:
        requested_items = canonical_items(request.items)
        fingerprint = request_fingerprint(request.manifest_version, requested_items)
        order = self.store.find_order_by_request(request.checkout_request_id, fingerprint)
        if order is None:
            order = self._reconstruct_order(request, requested_items, fingerprint)
        start = self.store.start_or_load(order, nonce=uuid4().hex)
        if start.redirect is not None:
            return start.redirect
        if start.order_id != order.order_id:
            order = self.store.load_order(start.order_id)

        spec = CheckoutSpec(
            order_id=order.order_id,
            idempotency_key=f"cartray-checkout-v1:{order.order_id}",
            line_items=tuple((item.stripe_price_id, item.quantity) for item in order.items),
            success_url=self.success_url,
            cancel_url=self.cancel_url,
            metadata=projection_metadata(
                order_id=order.order_id,
                catalogue_version=order.manifest_version,
                items=tuple(CanonicalItem(item.product_key, item.quantity) for item in order.items),
                nonce=start.nonce,
            ),
        )
        redirect = self.gateway.create_checkout(spec)
        self.store.attach_redirect(start.order_id, redirect)
        return redirect

    def _reconstruct_order(
        self,
        request: CheckoutRequest,
        canonical: tuple[CanonicalItem, ...] | None = None,
        fingerprint: str | None = None,
    ) -> CheckoutOrder:
        if request.manifest_version != self.catalogue.version:
            raise CheckoutValidationError("catalogue refresh required")
        canonical = canonical or canonical_items(request.items)
        items: list[OrderItem] = []
        currency: str | None = None
        for requested in canonical:
            product = self.catalogue.product(requested.product_key)
            if requested.quantity > product.source.max_quantity:
                raise CheckoutValidationError(f"quantity exceeds policy: {requested.product_key}")
            if currency is None:
                currency = product.price.currency
            elif currency != product.price.currency:
                raise CheckoutValidationError("mixed checkout currencies are not supported")
            items.append(
                OrderItem(
                    product_key=requested.product_key,
                    quantity=requested.quantity,
                    stripe_price_id=product.price.stripe_price_id,
                    unit_amount_minor=product.price.amount_minor,
                    fulfilment_resources=product.fulfilment_resources,
                )
            )
        order_items = tuple(items)
        digest_items = tuple(CanonicalItem(item.product_key, item.quantity) for item in order_items)
        return CheckoutOrder(
            order_id="cr_" + uuid4().hex,
            checkout_request_id=request.checkout_request_id,
            request_fingerprint=fingerprint or request_fingerprint(request.manifest_version, digest_items),
            manifest_version=request.manifest_version,
            items=order_items,
            items_digest=items_digest(digest_items),
            currency=currency or "",
            subtotal_minor=sum(item.unit_amount_minor * item.quantity for item in order_items),
        )
