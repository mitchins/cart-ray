# Milestone 4a contract

## Scope

Milestone 4a adds the test-only Stripe webhook settlement kernel. It does not deploy a Worker or
Pages project, apply a remote migration, configure a Stripe webhook endpoint, add a Queue consumer,
or call Valet. The webhook is a settlement trigger, not sufficient settlement evidence on its own.

The completion criterion is:

> A genuine or simulated signed Stripe Checkout completion causes exactly one CartRay order to
> transition from its immutable checkout snapshot to a cryptographically attributable,
> Stripe-reconciled `OrderConfirmed` state with an atomic outbox event. Malformed, forged, stale,
> duplicated, cross-environment, or semantically inconsistent inputs fail closed.

## Settlement evidence

For a supported `checkout.session.completed` event, CartRay performs this sequence:

```text
authenticated Stripe event
        -> identify Checkout Session
        -> authenticate CartRay projection on the Session
        -> retrieve current Stripe Session and every line-item page
        -> reconcile those facts with the immutable D1 order snapshot
        -> evaluate the Session-level settlement predicate
        -> atomically confirm the order and append OrderConfirmed to the outbox
```

Each layer establishes a distinct fact:

| Evidence | Establishes |
| --- | --- |
| Stripe webhook signature | Stripe delivered the event to this endpoint. |
| CartRay Ed25519 projection signature | CartRay authored the compact commerce projection. |
| Stripe Session and line-item retrieval | The current Stripe checkout facts. |
| Immutable D1 snapshot | What CartRay intended to sell. |

`checkout.session.completed` is a trigger only. CartRay never settles from its abbreviated event
object without retrieving the canonical Session and all line items.

## Ingress

`POST /stripe/webhook` accepts a bounded raw request body and requires a valid `Stripe-Signature`.
Verification uses the configured endpoint secret, Stripe's signed timestamp payload, an explicit
timestamp tolerance, every supplied `v1` candidate, and constant-time comparison. Invalid,
malformed, or stale signatures receive no settlement attempt.

The event must have a unique `event.id`, be test mode (`livemode == false`), and identify a payment
mode Checkout Session. The test-only Stripe API key used to retrieve the Session is account-scoped;
that account-scoped retrieval, together with the endpoint secret, is the direct-account binding for
this milestone. A future Connect-aware adapter must add an explicit connected-account comparison.

Verified unknown event types are recorded as ignored and return success. Unverified events do not.

The event ledger retains a SHA-256 of the raw body and processing state. An identical retry resumes
unfinished processing and is a no-op only after processing completes. Reuse of an event ID with a
different raw-body hash is a hard inconsistency.

For an authenticated event that is rejected during settlement, the internal event record may retain
one fixed, non-secret operator diagnostic naming the failed stage. It is written only while the
same event ID, Session ID, payload hash, and `received` processing state still match; it neither
changes the retryable state nor appears in the webhook response. A later successful confirmation
clears it. The codes are private operational evidence, not a customer-facing contract; future
operators must tolerate unknown codes.

## CartRay projection

The retrieved Checkout Session metadata is the canonical Stripe-carried CartRay projection. Its
PaymentIntent metadata remains only a redundant paid-order diagnostic projection.

M4a accepts only supported schema-1 `cr_*` fields, including the existing key name `cr_kid`. It
requires `cr_source=cartray`, a supported schema, one known allowed public key, a valid Ed25519
signature bound to the retrieved Session ID, bounded contiguous item chunks, canonical positive
quantities, no duplicate product keys, and a matching reconstructed items digest. Unknown or
non-canonical CartRay fields fail closed.

The projection order ID must resolve to exactly one immutable D1 order. Its nonce, catalogue
version, items digest, and canonical product quantities must equal the persisted checkout snapshot.
The event Session ID, retrieved Session ID, D1 external Session ID, and projection-bound Session ID
must all agree.

## Stripe/D1 reconciliation

CartRay retrieves every Stripe line-item page with bounded pagination, repeated-cursor detection,
duplicate-line detection, and an item limit. It compares the exact multiset of Stripe Price IDs and
quantities to the immutable D1 order items, and requires matching currency.

Stripe owns taxes, discounts, and final settlement totals. CartRay does not compare `amount_total`
to the original catalogue subtotal. It records Stripe's settlement facts while retaining the stricter
Price/quantity/currency comparison. This permits a paid catalogue cart discounted to zero without
confusing catalogue value with payment required.

## Settlement state and idempotency

Settlement state is separate from the existing checkout-creation state machine. An append-only
migration records `pending -> confirmed`, the Stripe Session and event IDs, and Stripe-owned
settlement facts. A D1 atomic compare-and-set performs the transition and inserts:

```text
OrderConfirmed(cr_order_id, stripe_session_id, items_digest, ...)
```

into the outbox in the same transaction. No outbox consumer exists in M4a.

There are two idempotency boundaries:

1. An event-level ledger keyed by `stripe_event_id` and raw-body hash.
2. A business-level confirmation keyed by CartRay order and Stripe Session.

An already-confirmed order for the same Session succeeds as a no-op. An attempt to confirm the
same CartRay order with a different Session is a hard inconsistency requiring investigation.

## Future M4b configuration gate

M4a ships no hosted endpoint or secret configuration. Before M4b deploys this route, the Worker
must receive `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`,
`CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64`, and `CARTRAY_PROJECTION_PUBLIC_KEYS_JSON` as secrets,
never Wrangler vars or repository files. The public-key secret is a JSON object mapping the existing
`cr_kid` values to standard-base64 Ed25519 SPKI public keys. `CARTRAY_ENVIRONMENT` remains exactly
`test`; a test-mode endpoint secret and a Stripe `rk_test_` or `sk_test_` key are required.

Stripe's endpoint must send `checkout.session.completed` to `/stripe/webhook`. CartRay must first
run the remote `0002_webhook_settlement.sql` migration and then prove a signed test delivery before
any readiness decision. This is an activation checklist only, not authorization to deploy from M4a.

## Settlement predicate

For v1, a retrieved Session settles only when:

```text
status == complete

AND
(
  amount_total > 0 AND payment_status == paid
  OR
  amount_total == 0 AND payment_status IN (paid, no_payment_required)
)
```

This explicitly includes both native free carts and paid catalogue carts reduced to zero by a Stripe
discount. Stripe can report a completed zero-total Session as either `paid` or
`no_payment_required`; a missing PaymentIntent is never used as a free-order signal. Delayed or
unpaid payment states fail closed in M4a; their asynchronous event support is a later contract.

## Required test matrix

The kernel must prove paid, native-free, discounted-to-zero, and quantity-five settlement. It must
also prove invalid or stale signatures; unknown event handling; event-hash conflicts; every relevant
projection/D1 mismatch; multi-page line items; transient retry recovery; distinct events for one
Session; and a different-Session confirmation conflict. The current checkout contract rejects
duplicate browser product keys; M4a does not introduce browser-side aggregation.

## Explicit non-goals

Valet independently consumes Stripe's authenticated CartRay projection through its own Stripe
adapter. M4a does not add a CartRay-to-Valet HTTP callback. Deployment, Stripe Dashboard setup,
remote D1 migration, hosted checkout, and the first operational evidence chain are M4b.
