# Milestone 5b sandbox acceptance evidence

## Scope and boundary

This records the post-Checkout return-flow proof for the merged Milestone 5a code. It remains
strictly Stripe test mode and Cloudflare test infrastructure. It is not a live-mode promotion,
customer order dashboard, receipt system, fulfilment integration, or Valet activation.

## Deployed revision

| Component | Test deployment |
| --- | --- |
| Worker | `cartray-test` version `aad48ba6-86d5-450b-b79f-08176feab370` |
| Worker URL | `https://cartray-test.mitch-336.workers.dev` |
| Pages deployment | `https://0c9a8512.cartray-store-test.pages.dev` |
| Pages production URL | `https://cartray-store-test.pages.dev` |
| Storefront API base URL | `https://cartray-test.mitch-336.workers.dev` |

The deployed Worker `/health` response remained test-only. A CORS request from the Pages origin to
the read-only status route returned only `{"state":"confirmed"}` for an already-confirmed test
Session.

## Genuine sandbox return checks

On 2026-09-03, the operator completed both test-mode Checkouts through the deployed Pages
storefront. Stripe returned the browser to the Pages success view, which displayed `Order
confirmed.`. The outcome below is from a remote D1 query joining the recorded checkout and webhook
event state; no secret values or customer details are retained here.

| Case | CartRay order | Stripe Session | Stripe event | Total minor | Settlement / event | `OrderConfirmed` rows |
| --- | --- | --- | --- | ---: | --- | ---: |
| Paid digital template | `cr_54b8aa54d28f496b9e23d74b8bcd454f` | `cs_test_b1vozHUW5tXosYrb9HR9xsBEygLUP8yblHcAniPXGPbkmoS1MD0I96yDwl` | `evt_1UBQqMBLB2xuBxraXT1h687g` | 2500 | `confirmed` / `confirmed` | 1 |
| Native free resource | `cr_a34342d2e1df4489bca3a98cc7de64c5` | `cs_test_b1pN4DcPT2Yg99V5fxLHU63qvxzB73ueansiBuF5RHfPLhn0jamx1pFiPd` | `evt_1UBQosBLB2xuBxraexHZzwy9` | 0 | `confirmed` / `confirmed` | 1 |

For both cases, D1 recorded `stripe_payment_status=paid`, no processing error, and exactly one
`OrderConfirmed` outbox row. This demonstrates that the return view observed the existing webhook
settlement state; it did not create a second settlement or outbox transition.

## Conclusion

M5b is complete as a sandbox acceptance proof for the minimal return experience. The normal
settlement authority remains the signed Stripe webhook; the browser status read is deliberately
non-authoritative and contains no order detail. Further customer-facing work, fulfilment, Valet,
and every live-mode concern remain separate milestones.
