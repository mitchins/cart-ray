# Milestone 9b contract: authoritative Checkout expiry

## Scope

M9b adds an authoritative `expired` Checkout state without weakening the browser boundary.
Stripe sends a signed `checkout.session.expired` event to the existing webhook endpoint; CartRay
verifies that delivery, resolves its exact persisted Checkout Session, and records the D1 transition:

```text
pending → expired
```

The existing D1-only `GET /checkout-status` route may then return `{"state":"expired"}`. The
storefront can unlock its retained cart only from that recorded state. It does not retrieve Stripe
while polling, and neither does the browser.

M9b is test mode only. It does not grant fulfilment, emit `OrderConfirmed`, change Valet, add an
order dashboard, expire a Stripe Session itself, or implement scheduled reconciliation.

## Expiry-event processing

For an authenticated test-mode `checkout.session.expired` delivery:

1. the event must contain one valid Checkout Session ID;
2. that ID must resolve to exactly one CartRay `checkout_sessions.external_session_id` row;
3. CartRay atomically records the authenticated event and changes the row from `pending` to
   `expired`, retaining the first expiry event ID and timestamp for forensics; and
4. no outbox event is produced, because expiry is neither payment nor fulfilment.

The event ledger preserves its first raw payload hash. A repeat delivery with the same event ID,
or a later authenticated duplicate expiry event for an already expired Session, is harmless. An
unknown Session, event-ID identity conflict, or `confirmed → expired` attempt is an inconsistency
and receives the existing rejected-webhook response. `expired → confirmed` is likewise forbidden.

`checkout=cancelled` remains a browser-return fact, not this D1 state. It may unlock the local
cart, while an authenticated later `checkout.session.completed` is still processed normally.

## Browser status and experience

`/checkout-status` retains its exact-origin CORS policy, `Cache-Control: no-store`, opaque Session
identifier, and D1-only read behavior. Its successful state vocabulary becomes:

```text
pending | confirmed | expired
```

For `expired`, the storefront retains the cart, removes the Checkout return query, announces that
the old Checkout expired, and enables product and Checkout controls for a fresh purchase. Pending
and unknown responses remain locked; a failed payment is not invented as a Checkout Session state.

## Activation and later reconciliation

After deployment, the operator must add `checkout.session.expired` to the existing **test-mode**
Stripe event destination for `/stripe/webhook`. The existing signing secret remains valid if the
same destination is edited; creating a replacement destination requires its new secret to be
deployed before sending events.

A later reconciliation slice may inspect stale D1-pending Sessions with a scheduled, server-side
Stripe retrieval and repair a missed completion or expiry event. That safety net is deliberately
outside M9b and never runs on browser status polling.

## Acceptance criteria

M9b proves that:

1. a verified, known expiry changes `pending` to `expired`, records an expiry ledger entry, and
   produces no `OrderConfirmed` outbox event;
2. duplicate expiry delivery is idempotent;
3. unknown Sessions and expiry after confirmation are rejected without changing the confirmed
   order;
4. completion after an authoritative expiry is rejected;
5. `/checkout-status` exposes `expired` through its existing D1-only route; and
6. an expired return retains but unlocks the cart for a fresh Checkout, while pending and unknown
   outcomes stay locked.
