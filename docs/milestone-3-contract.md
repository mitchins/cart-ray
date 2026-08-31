# Milestone 3 contract

## Scope

Milestone 3 adds a static Cloudflare Pages test storefront and the existing CartRay Worker's
browser-facing catalogue boundary. It remains test-only: it does not deploy a Pages project or a
Worker, configure a real storefront origin, receive Stripe webhooks, or call Valet.

## Public catalogue

`GET /catalogue` loads a catalogue independently of checkout/D1/signing configuration. Its
response is an exact allowlist:

```text
version
products[]: product_key, title, amount_minor, currency, max_quantity
```

Only active products appear. The endpoint never exposes Stripe Price IDs or lookup keys,
fulfilment types, versions, resources, or credentials. Display price and currency are public
catalogue facts; they are not accepted back from the browser during checkout.

## Browser and CORS boundary

The Worker requires the deployment-time `CARTRAY_STOREFRONT_ORIGIN` setting for `/catalogue` and
`/checkout`. It must be one exact HTTPS origin, configured with the Worker deployment rather than
committed as an operational value. A matching `Origin` receives route-specific CORS headers,
`Vary: Origin`, and an `OPTIONS` response with status `204`. Only `Content-Type` is permitted as
a preflight request header.

Any other origin is rejected before the catalogue or checkout factory runs and receives no
allow-origin header. Requests with no `Origin` may use these routes without CORS headers; CORS is
not authentication.

The checkout body is unchanged and remains exactly:

```text
checkout_request_id
manifest_version
items[]: product_key, quantity
```

All Stripe/financial/fulfilment decisions remain server-authored.

## Pages storefront

`storefront/` is a dependency-free static Pages application. Its build writes only static assets
to the ignored `storefront-dist/` directory.

By default, and for every preview build, the generated configuration has checkout disabled and a
synthetic public fixture catalogue:

```json
{"checkoutEnabled": false, "apiBaseUrl": null, "previewCatalogue": {"version": "sha256:preview-fixtures-v1", "products": [...]}}
```

Preview checkout performs no POST request, even when an API-base environment variable is present.
It still renders its fixture products and supports local cart interactions, so the UI can be
reviewed by hand at `npm run preview:storefront` without a Worker, Stripe, or remote deployment.
A production Pages build requires both `CARTRAY_STOREFRONT_MODE=production` and an HTTPS
`CARTRAY_STOREFRONT_API_BASE_URL`; that public build setting points to the Worker. No API URL is
committed. The Worker origin setting and Pages production build setting are configured separately
when the test deployment is activated.

## Acceptance gate

CI verifies the active-only catalogue allowlist, sensitive-field redaction, CORS success/error/
preflight matrix, and that rejected origins do not invoke a factory. It builds both preview and
synthetic-production storefront configurations, verifies the preview has no checkout endpoint,
and proves the production client posts only the CartRay checkout browser contract.
