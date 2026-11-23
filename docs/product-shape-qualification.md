# PRODUCT-SHAPE-01 qualification receipt

Scope: isolated, test-only Grove Demo Store, 23 November 2026. This is not a
production release or a deployment to the retained Effective Policy UAT store.

## Implemented product

One order confirmation, one optional order-level follow-up, one configured
private email provider. Review settings are off/receipt/followup/both and
all/priority. Selection does not restrict purchase eligibility. Capabilities
exist before provider acceptance; follow-up uses current review progress and
freezes selection at its sole claim. Receipt delivery survives optional Reviews
failure. Ambiguous delivery claims never authorize automatic resending.

New installation topology: Commerce, optional native Reviews, one private mail
handler; one physical merchant D1, zero queues. Pages owns static storefront
assets and the narrow customer gateway. The old intake producer/consumer,
Reviews scheduler/senders, OTP authority and per-workflow email execution paths
are removed. Historical migrations, issued capability compatibility and stored
claims remain. No retained EP resource, credential, checkpoint or uncertain
claim was changed. Queue-era inventory stops with a pinned-release diagnostic;
this release does not silently migrate that installation.

## Actual hosted evidence

- Personal Cloudflare account `33623e6d0b4793842a1832c5512aeb49`.
- Workers: `grove-shape-test-source`, `grove-shape-test-reviews` (private),
  `grove-shape-test-post-purchase` (private).
- Exactly one D1: `bbb3526e-3df2-4a60-9436-f0b1ba156770`; no queues.
- Storefront: https://grove-shape-test-store.pages.dev/ . Private dummy R2
  bucket `grove-shape-test-files`, without public asset access.
- Existing approved Stripe sandbox `acct_1UYzlzPheThyzcxa`, test mode only.
- Catalogue `sha256:f5cbaff5e0909d23b169c910cd76e0cf144e1f9369df18d81d15365cd59a8e67`.
- Order fingerprint `af614f799aa3f28b16b4195637f0338353fd3f0ef8f2056e918fb284955c9460`:
  Toolkit quantity 2 and Planter quantity 1, AUD66. Authoritative payment was
  complete/paid, live mode false, without a storefront return.
- One accepted CartRay confirmation, provider reference
  `1xVNwv-FnQW0hPurRF-ZaOh`, received in the approved inbox. Its stable
  accountless order link exposed both purchased items and four resource entries:
  one private dummy file and three external links. A fresh HTTP client retrieved
  the correct file bytes/name without checkout cookies; an unrelated token was
  rejected. No paid merchant materials were used.
- Toolkit review submitted through native Reviews, moderated and published with
  a five-star aggregate on its actual product page.
- One received follow-up featured only the outstanding Planter, provider
  reference `1xVOHM-FnQW0hPlysX-ecaz`. Native progress retained the submitted
  Toolkit. Raw received mail contained HTTPS one-click unsubscribe headers with
  DKIM coverage. The original approved mailbox's existing merchant suppressions
  were not cleared.
- Replayed the actual signed completed event twice: both HTTP200, no new
  confirmation. Repeated scheduled processing produced no duplicate follow-up.
- Fresh activation watermark 0 remained 0, subscriber cursor reached 3. Build,
  additive migrations, paused upgrades and re-verification retained keys,
  purchase, review and delivery state. No historical mail backfill.

## Limits of the hosted evidence

The first receipt reached the inbox with order access but omitted direct review
links because its optional enrichment exceeded a two-second budget. Capabilities
already existed and were reachable through the order page. The budget was
corrected to four seconds. A second existing AUD42 sandbox Checkout is open to
verify direct receipt inclusion after that correction. Browser security blocked
further payment interaction; the owner handoff is pending. No API or alternate
browser was used to bypass that restriction, and the unpaid order is not proof
of successful qualification. Provider uncertainty/cancellation and all timing,
selection, consent, suppression and recovery races are executable local
Workerd/D1 or unit evidence, not induced live SMTP uncertainty.

The follow-up delay was shortened to 432 seconds for this test. Maintenance,
pauses and diagnosis delayed observed sending; this run is not a timing SLA.
The original scheduled coroutine bug was corrected and the real scheduled
invocation completed delivery. No production withdrawal, performance or email
SLA is claimed. Manual screen-reader release checks remain in issue #77.

## Operator and release boundaries

Use the documented schema-2 installer and merchant example in
`modules/reviews/operator/`. Apply additive migrations before the new source;
retain the installation journal and keys. Verify deployed bindings after build
and deployment, then deliberately activate test delivery. Finish qualification
with `pause`. Checkout, resource access, management and unsubscribe remain
available while sending is paused.

Rollback is application-only to compatible schema/resources with mail OFF;
never rewind D1 or run legacy workflow senders beside the new receipt processor.
The migration prevents legacy and new confirmation claims for the same order.
Operator investigation is required for unknown provider outcomes; no blind
resend is provided. EP credential replacement/handover, production mappings,
channel cutover and live activation remain separately authorized work.

Exact-head CI and independent review dispositions are recorded on PRs #95/#96.
