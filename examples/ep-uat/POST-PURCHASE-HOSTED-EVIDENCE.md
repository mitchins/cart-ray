# Hosted post-purchase test receipt

Qualification on 18 November 2026 used only the approved Stripe sandbox,
approved test mailbox, and a dummy Dropbox transfer. It did not use production
payments, paid Effective Policy material, production collection or customer
data. The Stripe Checkout success URL was a neutral page; no CartRay storefront
return or browser-triggered reconciliation was used.

## Isolated resources and artifact

- Cloudflare account: `33623e6d0b4793842a1832c5512aeb49` (personal test account).
- Commerce D1: `ep-post-purchase-test-a1c9d6`, ID `6c87503c-805b-4482-a60e-0ffa5c381973`.
- Source Worker: `ep-pp-test-source-a1c9d6`, exercised version `99b72417-825e-44a3-9f08-ae9952110050`.
- Private handler: `ep-pp-test-handler-a1c9d6`, exercised version `7654fcd6-64c4-4638-8906-c9f057d4e280`. Its public workers.dev route returned 404.
- Stripe sandbox account: `acct_1UYzlzPheThyzcxa`; its test API credential was independently checked against the approved test-price fixture. The disposable Stripe webhook endpoint was removed after qualification.
- Clean commerce build source: `ae784e6e2f42fcd11a96fe95a1df09e4bde1a45e`; retained bundle SHA-256 `af23176c10f18ce8cf6ad38dc8497d00797fd72ac08d1142131099f0eaa444cc`. The application and migration files match the later PR head; changes since that build are tests, the operator inspection interface and documentation.
- Handler SHA-256 `16ceea56d2571aabafeb915c2e5327fa1e455369eff420cc352fdc941c73fbf9`; recovery migration SHA-256 `5c6ad1985b4ea0401bf3e66cb340d0dcd32bac5b20a87c1f932d38235ae50e2e`. Wrangler `4.127.1`, `kinglet` `1.8.3`, and `workers-runtime-sdk` `1.8.1` were used.

## Observed journey

The activation watermark and initial checkpoint were both zero in the empty
test D1. A confirmed, paid test Checkout for one authorised `EP-UAT-171` offer
settled at 02:09:53 UTC. The order identity SHA-256 was
`ebe5ad5bd0bc3eeed5e38bd3c554ab4f830e1bc57414e74e4f2a072e9acf1b6d`;
the immutable items digest was
`sha256:54e54d36fd2c643c7b4008b5b469d86db7e6dd2d82d1f2c3e68213278ef106d9`.
One `OrderConfirmed` event led to one `ep-dummy-delivery` claim at 02:09:55
and accepted provider receipt at 02:09:56. The provider-reference SHA-256 is
`5dc51fa44cf56e8937fc1652df6519a5904b381a7b489cf8287c564cede18a23`;
the approved mailbox contained one matching dummy-link message at 02:09:56.
Provider acceptance and mailbox receipt are separate observations.

Stripe resent the original event to the isolated endpoint. A hosted Worker
trace observed the replay return HTTP 200. A scheduled invocation was observed
at 02:15:42 UTC with outcome `ok`. After both, D1 still contained one real
confirmed event and one accepted execution; mailbox search returned one matching
message. The checkpoint was `5`.

## Recovery and negative case

With test delivery paused, two explicitly synthetic confirmed records were
inserted: one matching action with a deliberately aged `claimed` execution,
followed by an unmatched product. A signed synthetic wakeup returned HTTP 200;
the checkpoint remained at `0`, the claim remained `claimed`, and no execution
was created for the later order. After pausing the Worker again, the operator
CLI inspected and held that claim. D1 recorded one immutable audit row and an
`uncertain` state. A second wakeup advanced the checkpoint to the later event
at `4`; the unmatched order still had zero executions. No provider action was
made for either synthetic record. The first state was injected to exercise
recovery; it was not represented as a real Stripe purchase.

The final test source and handler were deployed with delivery disabled. The
test schedule was removed and verified absent through the Cloudflare API. The
test D1 and paused Workers remain available for review; their identities above
are not production resources. The operator-managed test credentials remain in
their existing secure store.
