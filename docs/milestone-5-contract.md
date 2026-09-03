# Milestone 5 contract

## Scope

Milestone 5 adds the minimal customer-facing return experience after a Stripe-hosted Checkout.
It remains **Stripe test mode only**. It does not add accounts, customer authentication, customer
profiles, an order dashboard, receipt delivery, fulfilment, Valet, a Queue consumer, or live-mode
configuration.

The webhook remains the only settlement authority. The return page only reads CartRay's existing
operational state; it never calls Stripe, settles an order, writes an outbox row, or grants access.

## Return correlation

Stripe replaces the literal `{CHECKOUT_SESSION_ID}` template in a Checkout `success_url` with the
created `cs_…` ID. CartRay appends this server-side to the configured success URL:

```text
https://<Pages origin>/?checkout=complete&session_id={CHECKOUT_SESSION_ID}
```

The browser never supplies, selects, or modifies either redirect URL. The fixed cancellation URL
remains:

```text
https://<Pages origin>/?checkout=cancelled
```

The Checkout Session ID is an opaque return-correlation identifier, not evidence of purchaser
identity or a general order capability. CartRay uses the existing unique
`checkout_sessions.external_session_id` to find the record, so M5 adds no return-token secret,
new secret binding, or customer-data table.

## Minimal status endpoint

`GET /checkout-status?session_id=cs_…` is a browser route protected by the same exact-origin CORS
policy as `/catalogue` and `/checkout`. It accepts no request body and permits no custom request
headers. It sends `Cache-Control: no-store`.

Its only successful response is:

```json
{"state":"pending"}
```

or:

```json
{"state":"confirmed"}
```

`confirmed` means only that the existing Stripe webhook settlement transition has been recorded.
`pending` means the Session is known to CartRay but that transition has not yet happened. A missing
or malformed Session identifier receives the same generic `404` response. The endpoint must not
return order IDs, product keys, quantities, prices, currency, customer details, payment details,
metadata, Stripe payloads, or error diagnostics.

This route queries D1 only. It must not call Stripe and must not change checkout, settlement, or
outbox state. CORS is not authentication; the intentionally tiny response prevents this opaque
identifier from becoming a customer-order API.

## Storefront behaviour

The static Pages application recognises the configured query values:

- `checkout=complete` with a valid Session ID displays “Confirming your order” and calls the
  status endpoint immediately, then with bounded backoff for at most 30 seconds.
- A `confirmed` response displays “Order confirmed”, clears the locally persisted cart, stops
  polling, and removes the Session ID from the visible URL with `history.replaceState`.
- A still-pending, malformed, unknown, or failed status lookup never clears the cart. The UI gives
  a generic confirmation-pending/error message and never exposes a backend error body.
- `checkout=cancelled` displays “Checkout cancelled”, removes the query string from the visible
  URL, and retains the cart.

The cart becomes browser-persistent only as a convenience. It stores the public catalogue version,
product-key/quantity pairs, and a monotonically increasing browser revision under a versioned
local-storage key. On load, the current public catalogue revalidates every saved product key and
clamps quantity to the published maximum; an unknown, malformed, or stale-version cart is
discarded. This state remains untrusted and the Worker retains the existing checkout validation
boundary.

After `/checkout` returns its existing server-issued `session_id` and before the browser navigates
to Stripe, the storefront stores a separate versioned pending-checkout correlation:

```text
session_id
catalogue version
canonical submitted cart snapshot
cart revision
```

On a confirmed return, the cart is cleared only when the returned Session ID matches that pending
correlation **and** the current validated persisted cart has the same snapshot and revision. A
foreign, stale, or revisited Session URL, or a cart changed in another tab while polling, still
displays the status but retains the cart. Once a matching pending checkout reaches `confirmed`, its
correlation is consumed so that an old return URL cannot perform future cart cleanup.

## Fixtures and acceptance tests

The implementation must add synthetic fixtures and prove:

1. Stripe Checkout receives the exact server-authored success URL template and unchanged cancel
   URL; the browser checkout payload remains unchanged.
2. A known pending Session returns only `pending`; a confirmed Session returns only `confirmed`;
   unknown and malformed identifiers return the same generic `404` with no operational data.
3. The status route obeys exact-origin CORS and preflight behaviour, rejects other origins before
   D1 access, sends `Cache-Control: no-store`, and performs no Stripe call or D1 mutation.
4. A success view polls pending to confirmed, clears the persisted cart only after confirmation,
   only for its matching unchanged pending-checkout correlation, and removes the Session ID from
   browser history.
5. A foreign or stale confirmed Session URL, and a cart changed during polling, retain the current
   cart and cannot consume an unrelated pending-checkout correlation.
6. Cancellation, unknown status, and polling timeout retain a validated persisted cart.
7. Malformed or cross-version persisted browser state is discarded; a saved excessive quantity is
   clamped to the public catalogue maximum.

The sandbox acceptance check is one paid and one native-free Checkout. Each must return to the
Pages success view, show `confirmed` after the genuine webhook, clear the cart only then, and leave
the Worker settlement/outbox counts unchanged by status polling.

## Explicit non-goals

M5 does not make return-page rendering a payment or fulfilment trigger. It does not expose a
recoverable customer order history, issue downloads, send email, accept a browser-authored Stripe
Session ID as a settlement command, or alter the Stripe webhook destination and secrets.
