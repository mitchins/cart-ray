# Milestone 9b sandbox acceptance evidence

## Scope and boundary

This records the M9b test-mode acceptance check against CartRay's existing Cloudflare Worker,
D1 database, Pages storefront, and Stripe webhook destination. It proves authoritative Checkout
expiry and a subsequent fresh purchase. It does not enable Stripe live mode, alter Woo, grant
fulfilment or Valet entitlements, or retain purchaser, card, or other payment details.

The browser status route remained D1-only throughout this check; it did not retrieve Stripe state
while rendering the return view.

## Deployment and webhook activation

| Component | Test deployment |
| --- | --- |
| Source revision | `12d83ab` (`Ingest authoritative Checkout expiry (#25)`) |
| Worker | `https://cartray-test.mitch-336.workers.dev` |
| Pages production URL | `https://cartray-store-test.pages.dev` |
| Stripe destination | `cartray-test-v2` (test mode) |
| Events | `checkout.session.completed`, `checkout.session.expired` |

Migration `0003_checkout_expiry.sql` was applied remotely before the check.

## Authoritative expiry and clean replacement

The operator opened a native-free Checkout for `EP-LMS-TRAINING-CATALOGUE`, then expired the
still-open **test-mode** Checkout Session with Stripe's API. Stripe delivered the signed
`checkout.session.expired` event successfully. CartRay stored the event, changed the checkout
from `pending` to `expired`, and did not emit an `OrderConfirmed` outbox row.

The returned Pages storefront then displayed the expired-return message, retained the cart, and
unlocked it for another purchase. The operator used that retained cart to start and complete a
fresh native-free Checkout. Stripe delivered the replacement `checkout.session.completed` event
successfully.

| Case | CartRay order | Checkout Session | Stripe event | Settlement / event processing | Item | `OrderConfirmed` rows |
| --- | --- | --- | --- | --- | --- | ---: |
| Authoritative expiry | `cr_c4f8bdf24aa24514ba88fcc2d08fe9f3` | `cs_test_b1blOhyiC9sWmXSOKOSiqnMmyoiekWSXKgSeYUnGGdC6EmkuuxiyBfjLbz` | `evt_1UBn9jBLB2xuBxraztOdjWHU` (`checkout.session.expired`) | `expired` / `expired` | `EP-LMS-TRAINING-CATALOGUE × 1` | 0 |
| Retained-cart replacement | `cr_9d7cb07f58324db2b81dc01ccb8e0e10` | `cs_test_b1C7n1s28UEdiP814dmjzei86fNRnZQWr8RB8UJPjyZtmaNii06YHzRlw6` | `evt_1UBnCgBLB2xuBxraluYLA9Zg` (`checkout.session.completed`) | `confirmed` / `confirmed` | `EP-LMS-TRAINING-CATALOGUE × 1` | 1 |

The replacement total was zero minor units and its Stripe payment status was `paid`, proving the
native-free Checkout path remained entitlement-capable without a PaymentIntent. The expired
Checkout remained non-confirming, while the later fresh Checkout produced exactly one operational
confirmation.
