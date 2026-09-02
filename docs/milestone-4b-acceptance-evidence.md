# Milestone 4b acceptance evidence

## Boundary

This is a recorded **Stripe test-mode only** operational proof. It neither enables Stripe live
mode nor authorizes production catalogue, fulfilment, Valet, Queue, or customer-data work.

The evidence was collected against:

- Worker: `https://cartray-test.mitch-336.workers.dev`
- Pages storefront: `https://cartray-store-test.pages.dev`
- Worker version: `65983396-cf5c-4ba4-8cdc-5d024ef5b3f7`
- Stripe API version: `2025-09-30.clover` for both CartRay requests and the configured test
  destination
- Stripe destination: `cartray-test-v2`, test mode, listening only to
  `checkout.session.completed`

The Worker secret listing exposed only the four expected names—`STRIPE_API_KEY`,
`STRIPE_WEBHOOK_SECRET`, `CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64`, and
`CARTRAY_PROJECTION_PUBLIC_KEYS_JSON`—and no values.

`uv run pywrangler d1 migrations list cartray-test --remote --config wrangler.toml` reported no
migrations to apply.

## Checkout and settlement evidence

Each row below is a genuine Stripe sandbox Checkout. For each order, D1 retained the shown
Stripe Session ID and the observed equality
`checkout_sessions.external_session_id = checkout_sessions.settlement_session_id`. The recorded
Stripe event is confirmed, has no processing error, and the transactional outbox contains exactly
one `OrderConfirmed` row. The webhook kernel rejects an event whose Session ID does not match the
persisted Session before it can create this state.

Settlement is recorded only after CartRay retrieves the Stripe Checkout Session and verifies
`status = complete` plus the amount/payment-status predicate. D1 does not independently store a
raw Stripe `status` value.

| Case | Stripe event | Order | Stripe Session | Stripe amount (minor) | Result |
| --- | --- | --- | --- | ---: | --- |
| Native free resource | `evt_1UB6FpBLB2xuBxraqlVSLwgh` | `cr_06af3c9f87ea4461a991666c968f6009` | `cs_test_b182QldK6va5TzTe4U6h6aGwtNmV3jNd6HnXst6KuLVJC8E43LHaSWSU7O` | 0 | Confirmed; a genuine resend returned HTTP 200 without another `OrderConfirmed`. |
| Paid digital template | `evt_1UBBQzBLB2xuBxracSazkhMd` | `cr_287216865eb24453bba048866d8a1b2d` | `cs_test_b1forLS2GPOCbGUjVZkgaDwCSwenEQMiKrjhhex7are5qW1LcxSfNkbdUC` | 2500 | Confirmed. |
| `CRTEST100` coupon reduces a paid item to zero | `evt_1UBBSpBLB2xuBxrahzK0eN0l` | `cr_efe4cd01dbdb4265b165ba8e9d50da0f` | `cs_test_b1SAIT0o2HItanMWG07JY3m3InlAKqdEEPFbyEKQXJXCf13H3IDLjr8Hyq` | 0 | Confirmed. |
| Support hours, quantity five | `evt_1UBBUaBLB2xuBxraAoyrjnii` | `cr_456c5f8ff9e34f7c9d9803018021a7e8` | `cs_test_b1OZXLqnLCt3EJm2IjuC1wpK3ytusJdHcoPuGKfkx0BzucIlYCfoZCkj0t` | 50000 | Confirmed; immutable item snapshot records `TEST-SUPPORT-HOURS` quantity 5. |
| Paid checkout after the Clover pin deployment | `evt_1UBLb1BLB2xuBxraKJvp2G4Q` | `cr_89ef541cd4bb447688e457eb31b35b01` | `cs_test_b1BYYG7DkxaI6SROkiECPpgfjkQ0psd0MjsJPlSOS1xglrhy9rOGm1ggAr` | 2500 | Confirmed. |

All five records have `stripe_payment_status = paid`, including the native-free and
coupon-reduced-to-zero Sessions. No PaymentIntent was required as the settlement predicate.

## Route and storefront observations

- `/health` returned HTTP 200 with `service=cartray`, `mode=test-only`, and `status=ok`.
- `/catalogue` returned HTTP 200 with the exact Pages origin in
  `Access-Control-Allow-Origin` and `Vary: Origin`; an `https://attacker.invalid` origin was
  rejected with HTTP 403.
- The test Pages storefront loaded the catalogue and redirected each checkout to hosted Stripe
  Checkout. Success and cancellation returned to the Pages origin as configured.
- The deployed Cloudflare Rate Limiting binding is `20` attempts per `60` seconds per location.
  A persistent Sydney test client making invalid checkout payloads received 50 HTTP 429 responses
  in an 80-request probe after the documented eventually-consistent counter converged. The probe
  did not create Stripe Sessions or write D1 orders.

## Remaining boundary

M4b is complete as a sandbox activation proof. The next customer-facing work is a separate
milestone: a minimal post-checkout confirmation/pending view. It must not turn this test Worker
into a live service, create a general order dashboard, or invoke downstream fulfilment.
