# Order access: test installation

This slice adds one purchase-scoped, accountless order page. It is test-only and
is disabled unless fulfilment is configured. It does not send email; the order
confirmation integration is the next slice. It does not activate Reviews or
change Stripe settlement.

For a fresh test installation using a merchant input package, add this field to
the existing operator configuration. Buckets are existing, private R2 buckets;
external-only merchants can use an empty object.

```json
"fulfilment": {"buckets": {"FILES": "merchant-test-files"}}
```

Add a `fulfilment_resources` JSON string to the protected operator credential
handoff. Keys match resource identities in the authoritative catalogue, not
browser-provided URLs. For example, the string can encode:

```json
{
  "resource-guide-v1": {"provider": "private", "binding": "FILES", "key": "guides/example.pdf",
                        "filename": "Guide.pdf", "mime_type": "application/pdf"},
  "resource-external-v1": {"provider": "external", "url": "https://example.test/resource",
                           "filename": "External resource"}
}
```

Run the existing `plan`, `provision`, `migrate`, `build`, `deploy`, and `verify`
commands. The installer creates and retains the order-access key, binds named R2
buckets to the existing source Worker and keeps the resource mapping in a Worker
secret. It reuses the installation's immutable merchant/environment identity;
it does not activate the post-purchase mail dispatcher. Keep the private vault
backup: existing access links continue to work after an upgrade only while their
D1 records remain, and issuing the same link again requires the original key.
If access records exist but the key is missing, restore it rather than creating
another key.

The trusted order processor calls `POST /internal/order-access` with the derived
issuer token, `{"order_id":"..."}` and a bounded content length. Only a
confirmed order with an `OrderConfirmed` event for the configured installation
can receive a link. The response is a relative path whose capability is in its
fragment, so the browser does not put it in the HTTP request URL. The customer
page posts the capability in the request body to list and retrieve only purchased
resources. A link is reusable and does not require an account. External links
can be forwarded once opened; CartRay cannot revoke a copied third-party URL.

The operator-owned Pages gateway forwards `/order-access` to the source Worker.
An externally owned storefront must route its configured source API prefix and
strip that prefix before forwarding `/order-access` to CartRay; no external site
is changed by this slice. For private-source deployments the issuer returns the
configured prefix in the customer path. The current operator can enable this
configuration for a fresh installation; adding it to an existing inventory needs
an explicit supported configuration update before the retained installation can
use it. No deployed environment or existing claim is changed by these examples.
