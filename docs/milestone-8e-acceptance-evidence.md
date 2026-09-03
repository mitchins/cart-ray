# Milestone 8e sandbox acceptance evidence

## Scope and boundary

This records acceptance of the M8d deployment of CartRay's locked two-product real subset to the
existing **Stripe test-mode** Worker and Pages projects. It does not enable live mode, change Woo,
alter Stripe Products or Prices, grant fulfilment or Valet entitlements, or record customer
details, payment details, credentials, or private fulfilment resources.

## Deployed test subset

| Component | Test deployment |
| --- | --- |
| Source revision | `aefb946` (`Select locked real Stripe test catalogue for deployment (#22)`) |
| Worker | `https://cartray-test.mitch-336.workers.dev` |
| Pages production URL | `https://cartray-store-test.pages.dev` |
| Pages deployment URL | `https://7c2a3fe7.cartray-store-test.pages.dev` |
| Commerce catalogue version | `sha256:8d3674ae409653d9194f71fcdeb5f7b24f4d8442aac3fd966d0c2612aeba71ee` |
| Presentation version | `sha256:984dc69335d36cc25aea197754cc106722f029b932c27748aa0a31ba862ec1dc` |

The deployed public catalogue contained only the reviewed test subset:

| CartRay product key | Amount | Quantity limit |
| --- | ---: | ---: |
| `EP-LMS-TRAINING-CATALOGUE` | AUD 0.00 | 1 |
| `EP-SIL-2026` | AUD 850.00 | 1 |

Both reviewed local WebP product assets returned HTTP 200 from the Pages production URL. The
storefront configuration enables Checkout only against the test Worker.

## Genuine Checkout evidence

The operator completed one native-free and one paid Stripe Checkout through the deployed Pages
storefront. The following read-only D1 join correlates each Stripe event with its CartRay checkout
session, immutable order item snapshot, and `OrderConfirmed` outbox record. Identifiers below are
test-mode operational identifiers only; no purchaser details are retained here.

| Case | Stripe event | CartRay order | Session | Total minor | Immutable item | Settlement / event | `OrderConfirmed` rows |
| --- | --- | --- | --- | ---: | --- | --- | ---: |
| Native free LMS catalogue | `evt_1UBjHFBLB2xuBxraing5Md5c` | `cr_a2c82854c8f14ff0bc25d15a0a9910c7` | `cs_test_b1pcXO0iRvR9MFckbObU5RPgjSzXjJxjL2FHqzbvbW17WAW0KQF2txh7O6` | 0 | `EP-LMS-TRAINING-CATALOGUE × 1` | `confirmed` / `confirmed` | 1 |
| Paid SIL package | `evt_1UBjIIBLB2xuBxrauTTYJMUI` | `cr_be4b543e0ca744afb4ad31c1734030e0` | `cs_test_b1AY7oYxS5P0N90ugMpnbh4TkxN9q8pLu2Hf5vZ080gZM0Ifj6ENucbUtV` | 85000 | `EP-SIL-2026 × 1` | `confirmed` / `confirmed` | 1 |

Both rows have `stripe_payment_status=paid` and no processing error. The free Checkout therefore
remains entitlement-capable from `checkout.session.completed` without a PaymentIntent, while the
paid Checkout proves the real preflight-locked Price path. Both use the same signed Stripe webhook
settlement path and each produced exactly one downstream operational confirmation.

## Follow-up

The sandbox run identified a customer-return usability follow-up:
[#23: Define terminal checkout-return cart behaviour](https://github.com/mitchins/cart-ray/issues/23).
It must retain the existing protections against stale, foreign, and cross-tab cart clearing while
making successful and other terminal return outcomes clearer to the customer.
