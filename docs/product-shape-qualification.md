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

## Final hosted continuation

The owner completed the existing second AUD42 Checkout. Stripe authoritatively
reported complete/paid, live mode false, for Toolkit and Planter quantity 1 each.
The same installation was rebuilt and deployed from implementation commit
`fa421ee9d8dc6d2df94efed8b68e92b2f4b70e6c`, preserving its keys and activation
watermark. Source version `8b0343ad-a71c-45d9-8e71-06f75dcadf99`, native Reviews
`85a6fc52-983b-4c1e-b962-b6959ee9b7f9`, private email handler
`47906112-2cee-453b-ac25-dbf7854dc54a`.

- Order fingerprint
  `9a33e349299c68521b0df1d53a816fdc040c09bc061a37646b2a89a7dae5b044`.
- Exactly one accepted confirmation for that order, provider reference
  `1xVQJD-FnQW0hPraPE-X38P`, actually received in the approved inbox.
- The received body contains both purchased items, stable resource access and
  the direct native review link, featuring Toolkit. Reading that link through
  the customer invitation API returned both eligible subjects, without checkout
  cookies or an extra permission step.
- The actual scheduled source invocation completed successfully in 931ms wall
  time and 112ms CPU. This is one observed invocation, not a performance SLA.
- Raw received headers contain List-Unsubscribe and List-Unsubscribe-Post with
  DKIM coverage. HTTPS one-click POST using the original order's unsubscribe
  link returned 200 without confirmation. The second order's pending follow-up
  became skipped, with no provider reference, while both transactional receipts
  and the original accepted follow-up remained unchanged. Suppression count 1.
- The retained activation watermark is still 0; the cursor reached 6, with two
  confirmation execution identities. No historical message was resent.

The first receipt had reached the inbox with working order access but omitted
its direct review enrichment after a two-second timeout. The corrected
four-second enrichment budget is now qualified by the actual second receipt;
the earlier browser-payment handoff is resolved.

One pause attempt observed stale Cloudflare deployment metadata immediately
after publication and stopped with its disable journal intact. Repeating the
supported pause command resumed that journal without replacing keys, resetting
claims or changing the checkpoint. Sending finishes OFF.

## Limits of the hosted evidence

Provider uncertainty/cancellation and all timing, selection, consent,
suppression and recovery races have executable local Workerd/D1 or unit
coverage. This run did not deliberately induce ambiguous acceptance at the live
SMTP provider. No real merchant materials, production purchases or campaigns
were used. The first order's repeated quantities and native submission,
moderation/publication and outstanding-product follow-up remain the hosted
end-to-end evidence; the second order proves immediate receipt inclusion and
cross-order unsubscribe suppression.

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
