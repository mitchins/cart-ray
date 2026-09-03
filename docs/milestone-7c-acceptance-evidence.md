# Milestone 7c sandbox presentation acceptance evidence

## Scope and boundary

This records the test-only deployment and acceptance check for the merged M7a/M7b catalogue
presentation compiler. It uses synthetic catalogue records, descriptions, and generated gradient
WebP covers only. It does not import business catalogue data or artwork, enable live mode, create
or modify Stripe objects, change fulfilment or Valet authority, or expose customer data,
credentials, Stripe Price IDs, or private fulfilment resources.

## Deployed revision

| Component | Test deployment |
| --- | --- |
| Source revision | `f748e84` (`Add synthetic catalogue presentation compiler (#18)`) |
| Worker | `cartray-test` version `eda84fb2-787b-419d-89bf-98ba5f9278ee` |
| Worker URL | `https://cartray-test.mitch-336.workers.dev` |
| Pages deployment | `https://6d335916.cartray-store-test.pages.dev` |
| Pages production URL | `https://cartray-store-test.pages.dev` |

The deployed Worker `/catalogue` response had the existing commerce catalogue version
`sha256:14dc0bcd5e3bac001f229446ffe0273dc6a6a9abfa63a832f0c2b6323608e12e` and the independent
presentation version `sha256:f9df98a40ac1a83944e74489edab771262e43836723b74524ea964d1c96fbd8b`.
It exposed four active synthetic products with public descriptions and local immutable image paths
only. All four referenced WebP assets returned successfully from Pages.

## Storefront smoke check

On 2026-09-03, the deployed Pages storefront rendered four product cards, four product images,
and the corresponding public short descriptions with no browser-console errors. This confirms the
new presentation fields reach the browser without becoming checkout inputs or a runtime external
media dependency.

## Genuine sandbox Checkout checks

The operator completed both test-mode Checkouts through the deployed Pages storefront. The remote
D1 query below joined each Stripe event to its Checkout Session, immutable order items, and the
`OrderConfirmed` outbox count. No customer details are recorded here.

| Case | CartRay order | Stripe Session | Stripe event | Total minor | Item snapshot | Settlement / event | `OrderConfirmed` rows |
| --- | --- | --- | --- | ---: | --- | --- | ---: |
| Native free resource | `cr_790e39ad4a4144709738ad6ca7e1b3e5` | `cs_test_b1SJA4Gx49T47mwBAa92NlsfdrzK99z6tixzLbLfhGaWfx4y6H7Vy6HHPz` | `evt_1UBYJrBLB2xuBxra4IUHi7Yb` | 0 | `TEST-FREE × 1` | `confirmed` / `confirmed` | 1 |
| Paid digital template | `cr_b876a5136645409399b2dd21a4ecb9a1` | `cs_test_b1sy1qBckYFKcJDfwWuWA2AT0BUhNbNXplYGgBv8s8Qda1jmAJ9J37mw2K` | `evt_1UBYKlBLB2xuBxra4Qc9XZXR` | 2500 | `TEST-TEMPLATE × 1` | `confirmed` / `confirmed` | 1 |

For both cases, D1 recorded `stripe_payment_status=paid`, no processing error, and exactly one
`OrderConfirmed` row. The zero-total case therefore remains entitlement-capable from
`checkout.session.completed` without a PaymentIntent, while the deployed presentation layer leaves
the signed Stripe settlement path unchanged.

## Conclusion

M7c proves the synthetic presentation compiler is deployed and works across the public catalogue,
static Pages assets, zero-total Checkout, and paid Checkout in sandbox. The next implementation
work is a deployable catalogue compiler that makes the reviewed CSV inputs the single source for
both Worker and Pages before any controlled real-product test subset is imported.
