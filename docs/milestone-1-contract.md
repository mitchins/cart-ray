# Milestone 1 contract

## Scope

Milestone 1 is deliberately confined to Stripe test mode. CartRay rejects non-test Stripe API
keys, and neither live Products nor live payments are in scope. The sandbox catalogue is resolved
by lookup key at runtime; Stripe Price IDs are never committed to this repository.

## Checkout Session sealing

Stripe does not provide a Checkout Session ID until after creation. Therefore the adapter follows
this order:

1. create the Checkout Session from CartRay's immutable, server-authored Checkout specification;
2. receive its `cs_...` ID;
3. construct and Ed25519-sign the canonical CartRay projection, including that Session ID;
4. write the complete sealed projection to Checkout Session metadata;
5. mirror that sealed projection to the PaymentIntent only when Stripe supplied a PaymentIntent;
6. return the Checkout URL to the buyer.

An error in steps 3-5 is a checkout failure: an unsealed Checkout Session URL is never returned.
Checkout Session metadata is canonical. PaymentIntent metadata is redundant diagnostic data only,
because free checkouts have no PaymentIntent.

## Projection signature

The signed, canonical JSON object contains the schema, source, Session ID, environment, CartRay
order ID, catalogue version, item count, item digest, nonce, and signing key ID. The final
production signer is an Ed25519 key held in a Worker secret, while Valet receives only its public
key. The adapter exposes a signer port so test fixtures cannot be confused with that production
key.

## Sandbox fixtures

The development account contains only these CartRay test objects:

| Lookup key | Price |
| --- | ---: |
| `cr_test_template` | AUD 25.00 |
| `cr_test_bundle` | AUD 50.00 |
| `cr_test_free` | AUD 0.00 |

`CRTEST100` is an active, once-only 100% **sandbox** promotion code. It exercises a checkout with
`payment_status=no_payment_required`; it is never a live discount.
