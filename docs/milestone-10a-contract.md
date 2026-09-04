# Milestone 10a contract — scheduled Checkout reconciliation

## Scope

M10a adds a test-mode-only safety net for an otherwise missed terminal Stripe Checkout state.
Once per hour, a Cloudflare Worker Cron Trigger may select a small, bounded set of stale CartRay
Checkout Sessions that are still `pending`, retrieve each exact Stripe Checkout Session on the
server, and repair D1 only when the retrieved object passes the same checks as normal settlement.

It does not alter the normal path:

```text
Stripe signed webhook → CartRay D1 → browser status
```

The browser continues to ask only what CartRay already knows in D1. No fetch route, including
`/checkout-status`, can invoke Stripe reconciliation.

M10a does not enable live mode, change the catalogue or checkout UI, expire Stripe Sessions,
create an order dashboard, deliver fulfilment, invoke Valet, or inspect customer/payment data.

## Authority and provenance

Stripe remains the financial authority. CartRay may learn a terminal state either from:

1. a signed Stripe webhook; or
2. a server-authenticated retrieval of an exact, already-persisted Stripe Checkout Session.

The second is a reconciliation observation, not a webhook. CartRay must never manufacture an
`evt_*` ID or put an API observation in `stripe_events`. Migration `0004` introduces a separate
`checkout_reconciliations` control/audit row for each persisted CartRay Session. A reconciliation
terminal transition legitimately has a null `settlement_event_id` or `expiration_event_id`; the
reconciliation row records the source and sanitized observation. A genuine later webhook is still
recorded in `stripe_events` and remains harmless.

## Candidate scheduling and control row

Only a row satisfying all of these conditions is eligible:

- `checkout_sessions.settlement_state = 'pending'`;
- it has a persisted `external_session_id`;
- it is older than the reconciliation grace period; and
- its reconciliation control row is due and not leased.

The production-test configuration uses one hourly UTC Cron Trigger. Each run claims at most five
candidates in deterministic due-time/order-ID order, processes them sequentially, and uses a
short opaque lease token. An overlapping Cron invocation cannot process a currently claimed row.
Each completed attempt stores a next-attempt time; `open`, non-terminal, rejected, and transient
results back off rather than causing the same oldest Sessions to be fetched every run. Existing
old pending rows are therefore activated gradually rather than in an unbounded first deployment.

The control row contains only operational data: attempt count, due/lease times, a fixed outcome or
diagnostic code, and sanitized Stripe terminal facts where applicable. It never stores a raw Stripe
payload, customer details, payment details, credentials, or a Stripe error body.

## Exact retrieval and verification

For a claimed candidate, CartRay retrieves only that row's `external_session_id`. Before any repair
it requires:

- returned ID equals the claimed Session ID;
- `livemode` is false and `mode` is `payment`;
- the signed `cr_*` projection verifies under CartRay's configured public-key ring;
- projection order ID, nonce, catalogue version, item digest, and canonical product/quantity list
  equal the immutable D1 order snapshot; and
- Stripe line-item Price IDs, quantities, unit amounts, and currency equal that immutable snapshot.

It deliberately reuses normal settlement's reconciliation and completion predicate. In particular,
discounts and tax remain Stripe-owned; CartRay does not compare a final Stripe total to the
pre-discount catalogue subtotal.

## Terminal classification and transitions

After exact verification:

| Retrieved Stripe state | CartRay action |
| --- | --- |
| `open` | Leave D1 pending and schedule a later check. |
| `expired` | Atomically transition `pending → expired`; produce no `OrderConfirmed`. |
| `complete` with the existing paid/native-free predicate | Atomically transition `pending → confirmed` and insert exactly one `OrderConfirmed` outbox row. |
| `complete` but not settled | Leave D1 pending and retry later. |
| Unknown/malformed/inconsistent state | Leave D1 pending and record a fixed, non-secret diagnostic. |

`confirmed` and `expired` never regress or cross-transition. The terminal checkout update, control
row update, Stripe-owned amount/payment facts, and any `OrderConfirmed` outbox insert occur in one
D1 batch/compare-and-set. Webhook-first, reconciliation-first, duplicate-cron, and late-webhook
races must converge to one terminal state and at most one confirmation outbox row.

## Activation and test evidence

`wrangler.toml` is the source of truth for the root Cron Trigger. Deploying its `crons` array
replaces the Worker's existing trigger set, so M10a declares exactly one test-only hourly trigger.
No new secret is required: the deployed test `STRIPE_API_KEY` already retrieves Sessions and line
items. The D1 migration is applied with the scoped D1 token before the Worker/trigger deploy using
the incumbent OAuth-backed terminal.

Local scheduled-handler verification uses Pywrangler/Miniflare's scheduled endpoint. Remote
acceptance deliberately withholds one test-mode terminal webhook, waits for the Cron repair, and
records D1 reconciliation provenance, the terminal state, and outbox cardinality. A normal webhook
completion is not M10a reconciliation evidence.

## Acceptance criteria

M10a proves:

1. stale candidate selection is deterministic, bounded, leased, and back-off-aware;
2. `open`, expired, paid, native-free, and unpaid-complete Sessions have the specified outcomes;
3. every Stripe identity, projection, immutable order, line-item, and environment mismatch fails
   closed without a terminal transition;
4. a recovered confirmation has one outbox row and no synthetic `stripe_events` record;
5. webhook/reconciliation races and later genuine webhook delivery remain idempotent; and
6. the scheduled handler is test-only while all browser routes remain D1-only.
