# Milestone 10a sandbox acceptance evidence

## Scope and boundary

This records the test-mode remote acceptance of M10a scheduled Checkout reconciliation against
CartRay's deployed Cloudflare Worker, D1 database, and Stripe test destination. It proves that
CartRay can repair two deliberately withheld terminal Checkout webhooks by retrieving Stripe from
the scheduled handler. It does not enable Stripe live mode, change the catalogue or browser
status-polling contract, grant fulfilment or Valet entitlements, or retain purchaser, card, or
other payment details.

## Deployment and controlled conditions

| Component | Test deployment |
| --- | --- |
| Source revision | `87cf391` (`Add scheduled checkout reconciliation (#26)`) |
| Worker | `https://cartray-test.mitch-336.workers.dev` |
| D1 database | `cartray-test` |
| Scheduled trigger | `0 * * * *` |
| Stripe destination | `cartray-test-v2` (`we_1UB69CBLB2xuBxrayoxwttci`, test mode) |

Migration `0004_checkout_reconciliation.sql` was applied remotely before the deployment. The
test destination was disabled before creating the two free Checkouts, then restored to `enabled`
after the evidence query. Consequently, the result below is reconciliation evidence, not a late
webhook delivery.

## Reconciliation outcome

The operator completed one native-free Checkout and expired the other prepared native-free
Checkout. The targeted test command made only those two D1 rows immediately eligible for the
next scheduled run. The hourly handler observed terminal facts directly from Stripe and completed
one reconciliation attempt for each row.

| Case | CartRay order | Checkout Session | Settlement state | Stripe observation | Reconciliation outcome | `OrderConfirmed` rows | `stripe_events` rows |
| --- | --- | --- | --- | --- | --- | ---: | ---: |
| Expired native-free Checkout | `cr_514ed96a96214555b340abb5b2700662` | `cs_test_b1I11wLWptUXSJLgI63zZI19s89NxI73ThLxH6Y3pG2VrUI1OQQUSpQZAa` | `expired` | `expired` / `unpaid` | `attempt_count = 1`; `last_outcome = expired`; no error | 0 | 0 |
| Completed native-free Checkout | `cr_1310d37c151149f2bb84ce514e01ff77` | `cs_test_b1iaqwRshpcpIFVQpEuokTNdhMYTDZ7QfCLds5Jm49ayO8iin1JCOHozDb` | `confirmed` | `complete` / `paid`; total `0` minor units | `attempt_count = 1`; `last_outcome = confirmed`; no error | 1 | 0 |

The confirmation created exactly one operational `OrderConfirmed` record without inserting a
synthetic Stripe event. The expired Checkout remained non-confirming. In both cases
`stripe_events` was zero because the destination was deliberately disabled; the scheduled handler
was therefore the source of the terminal D1 projection. This proves the missed-webhook recovery
path while retaining the existing rule that browser status polling remains D1-only.
