# Milestone 0 contract

## Boundary

The browser identifies only a product key and quantity. It cannot provide a price, Stripe Price
ID, fulfilment data, redirect URL, or `cr_*` metadata.

`CatalogSource` names a Stripe lookup key but does not contain a price. A `PriceResolver` turns
that lookup key into a resolved Stripe Price ID, amount, and currency. The fixture resolver is
used locally; a Stripe resolver replaces it in Milestone 1.

## Immutable business order

`orders` and `order_items` hold immutable facts: items, quantities, resolved Price IDs, unit
amounts, currency, manifest version, fulfilment expansion, and item digest. They are never
updated in place.

`checkout_sessions`, `stripe_events`, and `outbox` hold mutable processing state. A semantic
state transition and its outbox row are written in the same SQLite/D1 transaction.

## Canonical item projection

Product keys match `^[A-Z0-9][A-Z0-9_-]{0,63}$`. Items are lexicographically ordered by key;
each key occurs once and quantities are base-10 integers in the product's configured range.

The item digest is SHA-256 over this exact UTF-8 form:

```text
cartray-items-v1\n
PRODUCT_KEY:quantity\n
...
```

The metadata representation has `cr_item_count`, `cr_chunk_count`, and contiguous zero-padded
`cr_items_01` through `cr_items_NN` chunks. Parsing rejects missing chunks, duplicate keys,
incorrect counts, malformed keys, and digest mismatches.

## Milestone 1 sealing requirement

The production Stripe adapter creates a Checkout Session, receives its `cs_...` ID, and only then
signs a canonical projection containing the session ID, environment, order ID, catalogue version,
item count, digest, nonce, and key ID. It updates the Session metadata with that Ed25519
signature before returning the Checkout URL. An unsealed Session is never returned to a buyer.

PaymentIntent metadata is a redundant diagnostic projection only. Native entitlement processing
uses a sealed `checkout.session.completed` Session, never a PaymentIntent.
