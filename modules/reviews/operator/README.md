# Merchant test installation and operations

The supported deployment is Commerce, optional native Reviews and one private transactional
email handler, one merchant D1 and no queues. Pages supplies the storefront gateway when
the merchant does not own an external origin. A merchant-owned origin uses the existing
same-origin gateway contract. Core without Reviews remains independently deployable.
All operator activation is test-only; production release remains gated in issue #77.

## Prerequisites and inputs

Use the checked-in Node version, `uv sync --locked --all-groups` and
`npm ci --ignore-scripts`. Start with the checked-in merchant configuration example,
a verified Stripe **test** catalogue/Price lock and a generic merchant input package.
The build snapshots those inputs; builds do not infer prices, entitlements or subject IDs.

Supply one private JSON credential file outside Git, permissions `0600`:

```json
{
  "cloudflare_api_token": "OPERATOR_CREDENTIAL",
  "stripe_test_key": "sk_test_REPLACE",
  "smtp2go_api_key": "api-REPLACE",
  "approved_test_email": "approved@example.test",
  "test_sender": "verified@example.test"
}
```

The operator Cloudflare credential needs the selected account's Workers deployment,
D1, Pages and remote service-binding operations; no Queues permission is required.
Use R2 permission only when provisioning configured private object resources.
Management credentials stay on the operator computer, never in public Workers or Pages.
Stripe credentials must match the configured sandbox account and permit Checkout, Price
verification and a signed webhook. The email credential authorizes the verified sender
and the approved test destinations below. For `post_purchase_email_provider: "cloudflare"`, supply
`cloudflare_email_api_token` instead of `smtp2go_api_key`. There is one selected provider
for receipts, optional review follow-ups and customer-requested recovery.

Use schema 2 for the protected `post_purchase_config` file:

```json
{"schema":2,"sender":"verified@example.test","recipient":"approved@example.test"}
```

### Optional test-recipient allowlist

Without an allowlist, schema 2 retains the single `recipient` restriction above.
For a bounded UAT audience, optionally add `test_recipient_allowlist` to that same
protected schema-2 file:

```json
{
  "schema": 2,
  "sender": "verified@example.test",
  "recipient": "approved@example.test",
  "test_recipient_allowlist": {
    "addresses": ["specific@another.test"],
    "domains": ["merchant.test", "developer.test"]
  }
}
```

Both arrays are required, at most 32 entries each, with at least one entry overall.
The complete serialized delivery binding must also fit 4096 bytes; oversize lists
are rejected before configuration changes. Purchaser addresses are checked as ASCII
before case normalization, so Unicode lookalikes cannot fold into an approved address.
Entries are validated and case-normalized; duplicates, wildcards, URL syntax,
whitespace and unknown fields are rejected. Domains match exactly: `merchant.test`
does **not** allow `sub.merchant.test`, `evilmerchant.test` or `merchant.test.evil.test`.
An exact address does not allow plus aliases or other mailboxes at that domain.

This policy applies only in test mode. The shared private handler independently
retrieves the paid purchase and recipient from the configured Stripe account for
confirmation, Reviews evidence, follow-up and recovery. It never substitutes the
configured test mailbox for another purchaser or accepts a caller's recipient.
The existing `recipient` retains its original evidence spelling, including local-part
case, when Stripe identifies that same address. Existing consent, recovery and suppression
hashes therefore remain valid, with or without the new list. Other purchasers retain their
authoritative Stripe address; policy matching is case-insensitive, not a recipient rewrite.
The existing `recipient` and credential handoff remain the legacy fallback; when an
explicit allowlist exists, that list alone determines permitted test destinations.
It does not grant Reviews permission, bypass suppression or change live delivery.

To change an **existing paused** installation, supply only the two arrays in a
private `0600` JSON file outside Git:

```sh
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" pause
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" \
  reconfigure-test-recipients --config "$TEST_RECIPIENTS"
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" build
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" deploy
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" verify
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" doctor
```

The command verifies installed sending is OFF and the existing credential handoff
matches, then retains a content-addressed protected snapshot. Repeats are no-ops.
It creates no resource or key, performs no database mutation, and preserves sender,
provider, credentials, activation fence, checkpoint and every delivery claim.
Build/deploy remain OFF; enabling test delivery is a separate existing deliberate
activation action. Old releases with a different notification-policy fingerprint
are incompatible with activation/rollback until configuration is deliberately reconciled;
no database rewind or historical resend is performed. There is no EP deployment or
sending authorization implicit in using this command.

`review_requests` accepts `timing` (`off`, `receipt`, `followup`, `both`), `selection`
(`all`, `priority`), `delay_days` and an ordered `priorities` list. Defaults encourage
Reviews with the receipt and the highest-priority outstanding product. New checkouts
use the general permission “I'm happy to receive invitations to review products I purchase.”
(v3), independently of timing and product preferences. Historical one-request grants
cannot authorize a second request. Reviews suppression never removes the receipt. Private resource locators
belong in the protected `fulfilment_resources` credential value, not the public catalogue.

## One resumable workflow

In the following commands, `$INSTALL`, `$CREDS` and `$CONFIG` are operator-selected paths
outside the checkout. Inventory, release artifacts, migration checksums and the `0600`
secret vault persist there across terminal sessions and worktree removal.

```sh
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" plan --config "$CONFIG"
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" provision
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" migrate
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" build
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" deploy
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" verify
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" activate --enable-test-delivery
```

Inspect the planned account, environment and resource names before provisioning.
Repeat an interrupted command against the **same** inventory; do not create another namespace
as an upgrade. Unknown resource creation outcomes stop for reconciliation rather than claiming
similarly named resources. Migration errors retain `0600` logs and actionable retry diagnostics.
A verified new installation initializes its suppression key once. Missing or inconsistent
identity/key state in an existing installation stops sending and requires recovery.

Activation initializes the existing confirmed-order checkpoint at the current outbox maximum
once. No historical backfill occurs. Deploy and verification do not send. Pause leaves checkout,
order access and unsubscribe functional:

```sh
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" pause
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" doctor
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" receipt
```

## Upgrade, rollback and diagnosis

Pause first. Run `upgrade` from the reviewed new checkout; keys, orders, suppression and the
activation fence remain. Explicitly activate again after verification. `rollback --release N`
selects a recorded compatible **OFF** application release. It never rewinds D1 or runs destructive
down-migrations. An incompatible schema/key contract requires a forward fix or separate recovery.
Corrected merchant packages can be snapshotted with `build --merchant-inputs PATH` while OFF.

`doctor` verifies actual deployed versions, bindings, schema checksums, visibility and the retained
checkpoint. Resolve its specific diagnostic before activation. Provider uncertainty is inspected
and reconciled through `scripts/post_purchase_recovery.py`; inspect receipt/follow-up identity,
claim age and authoritative evidence. Never reset an ambiguous claim or blindly resend it.

Queue-era inventories and incomplete historical provider handovers are deliberately rejected by
this release without modification. Use their pinned release for exceptional retained operations.
This is not a migration of the retained EP UAT environment. Old handoff identities, credentials,
SQL history, issued capabilities and uncertain claims remain unchanged. There is no ordinary
separate Reviews sender activation, provider migration ceremony, OTP or queue recovery procedure.

## Updating the catalogue in an existing test installation

Keep the same installation directory, namespace, credentials and resources. Pause
sending first (or leave a never-activated installation OFF). Prepare an operator-selected
JSON file with exactly these four fields, using absolute local paths:

```json
{
  "merchant_inputs": "/private/merchant/full-package",
  "preflight_lock": "/private/merchant/full-stripe-test-lock.json",
  "subject_map": {"PRODUCT-ONE": "product-one", "PRODUCT-TWO": "product-two"},
  "subject_details": [
    ["product-one", "/product/one/", "Product one"],
    ["product-two", "/product/two/", "Product two"]
  ]
}
```

```sh
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" reconfigure-catalogue --config /private/merchant/catalogue-update.json
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" build
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" deploy
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" verify
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" doctor
```

The normal compiler validates exact test lookup keys, catalogue digest, presentation
and assets before changing inventory. Failures retain a protected compiler log.
This operation does not create Prices, approve fulfilment resources or modify
credentials, merchant identity, account, origins, provider, D1, keys or the activation
fence. Existing product-to-subject assignments and subject paths/labels cannot be
reinterpreted through this command. New mappings are explicit. Multiple offer keys may use one exact parent product
path only when `storefront-config.js` declares the same `reviewSubjects` identity
for each and the installer subject map agrees. `connected_flows.install` accepts
`review_subjects=subject_map` for this declaration. Each offer must retain a real
connected purchase action on that parent page; aliases do not authorise pricing
or erase variant identity. Different subjects, case conflicts and reserved routes
remain rejected. Retired subject rows
and purchased-item history are retained.

Package and lock snapshots are retained by content hash. Repeating the command is
idempotent; different inputs at the same source commit produce separate retained
release artifacts. Prior catalogue-version subject maps remain available to orders
created before an update. A catalogue version cannot reinterpret its mapping;
changed mapping content requires its corresponding explicit compiled revision.
Archived rollback uses recorded fingerprints and storefront integrity, not the
current checkout's client bytes. Build invalidation requires deployment and verification before
explicit test activation. Old Worker versions are checked against their recorded
OFF configuration while the replacement is being installed.

Compatible rollback restores the retained catalogue snapshot and application versions,
and republishes the corresponding storefront for operator-managed Pages. For an
externally owned site, it emits the retained artifact handoff; that site's operator
must coordinate its publication. Rollback does not rewind D1 or alter delivery claims,
suppression or the original no-backfill checkpoint.

## Private resource configuration size

The same protected `fulfilment_resources` JSON remains the only resource input.
Small values retain the original single secret binding. Larger values are encoded
into numbered secret bindings, each at most 4096 bytes, with a small count/hash
manifest. The Worker requires a complete, contiguous, matching set and retains the
existing 65536-byte decoded limit. These are private bindings in the existing
Commerce Worker, not public assets or a new storage service. Encoding is not encryption.
No credential or order-access key is replaced.

Deploy and rollback reject a source release that predates multipart support while
large configuration is selected. A compatible rollback retains the new backend
and its current resource bindings; do not downgrade to an earlier small-catalogue
runtime and claim it supports the enlarged resource set. Ordinary rollback never
rewinds purchased-item or suppression records.

## Evidence and limits

See `docs/order-reviews.md` and the checked-in qualification receipts for the exact tested build.
The test adapter remains recipient-restricted. Native staff moderation and capability APIs remain
private; the storefront gateway cannot expose administrative APIs. Shared D1 means Workers with a
binding have database-wide access, not physical table isolation. A complete D1 outage affects
Commerce and Reviews; an unavailable optional Reviews service does not block checkout or receipt.

## Refresh customer presentation without replacing the merchant installation

From the pinned new checkout, pause the existing **schema-2, no-queue** installation.
Do not select a similarly named queue-era installation. Create a public branding JSON input:

```json
{"name":"Example merchant","home_path":"/catalogue/","contact_path":"/contact-us/","accent":"#001b35"}
```

These four fields remain supported unchanged. Optional `logo_path` selects an operator-approved,
same-origin public SVG/PNG/JPEG/WebP asset path (no URL query or fragment; omit to disable); optional `heading_font`
selects an existing local stack: `modern`, `editorial`, `humanist`, or `system`. For example:

```json
{"name":"Example merchant","home_path":"/catalogue/","contact_path":"/contact-us/","accent":"#001b35",
 "logo_path":"/assets/merchant-logo.svg","heading_font":"editorial"}
```

The logo must already be approved and published by the merchant frontend. Validation checks
canonical image-path syntax, not remote availability or content ownership. Verify the existing
static asset in the frontend build before a deliberate rollout. No file upload, font download,
external style, arbitrary CSS, or HTML configuration is accepted. Omitting the two new fields
keeps legacy four-field binding bytes unchanged. Navigation paths remain same-origin directory
paths; accent is a six-digit hex colour. The native order page uses fixed responsive CSS, 48px controls and
contrast-safe text, without external styles/fonts, arbitrary HTML or a theme engine. Its
inline script/style are authorized by exact CSP hashes, not `unsafe-inline`. Existing form
POST/external-resource redirects and purchase authorization remain unchanged.

```sh
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" pause
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" \
  build --refresh-clients --order-access-presentation "$BRANDING"
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" deploy
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" verify
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" doctor
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" receipt
```

The build refreshes only shared client files and `orderAccessPresentation` in a temporary copy
of the hash-pinned current merchant package. It preserves generated JavaScript suffixes,
commerce bytes, offer/Stripe mappings, native subjects, canonical URLs and the original input
snapshot. It then uses the existing validated content-addressed input/build workflow.
`--merchant-inputs PATH` remains available for a separately regenerated merchant package;
it is mutually exclusive with `--refresh-clients`. Branding is an optional existing
`storefront-config.js` field, not a new vault, configuration registry or activation lifecycle.
`connected_flows.install(...,order_access_presentation={...})` supports the same contract.

No migration/provisioning is needed. Keep the existing credentials and protected recipient
policy. Orders, resources, keys, claims, activation watermark and subscriber cursor are never
rewound or re-enrolled. Compare pre/post receipts. The existing compatible application rollback
uses retained merchant snapshots, including their branding; it never restores old D1 data.
A merchant-owned frontend is composed/deployed independently from the generated artifact.

`pause`/`activate --enable-test-delivery` control the consolidated source/notification TEST
mode. Preserve the installation's existing Reviews authorization and publication state;
these are not permission to recreate or reset Reviews activation. Only under separate owner
authorization, restore the prior permitted TEST mode after verification with the existing
`activate --enable-test-delivery` command. Do not replace credentials, replay accepted/uncertain
claims, backfill, or enable production while refreshing presentation.


### Native order-page renderer upgrades

All native order pages receive the compact resource grouping on upgrade.
Logo/font/resource-group presentation is generated by the Commerce (`source`) Worker with
exact script/style CSP hashes. It is not an independently replaceable storefront asset.
The optional logo adds only `img-src 'self'`; fonts are fixed local/system stacks, with no
remote font loading. Resource filenames, POST forms and token/resource fields stay unchanged;
`.cr-order-item-header`, `.cr-order-item-title`, `.cr-order-quantity`, `.cr-order-resources`,
`.cr-order-resource` and `.cr-resource-name` are stable semantic hooks within the fixed renderer.
These render tests do not requalify or alter payment/entitlement/provider delivery authority.

This code delta affects only the source renderer, but the current supported installer `deploy`
updates **all installed roles** as one paused OFF release. There is no source-only selector.
Do not manually upload one Worker or patch the inventory to evade its release journal.
After separate deployment authorization, use the pinned checkout and existing
`pause → build --refresh-clients --order-access-presentation FILE → deploy → verify → doctor → receipt`
workflow. No provisioning or migrations are needed. Compare retained records/keys/fence/claims;
restore only the previously authorized TEST mode through the existing explicit activation.
Do not deploy this native renderer merely to consume an independent static cart/client update.
A compatible rollback uses the recorded release and its retained branding snapshot, never a D1 rewind.

## Approved historical snapshot publication

For an existing test installation, see [APPROVED_SNAPSHOTS.md](APPROVED_SNAPSHOTS.md)
for the explicitly authorised `apply-review-snapshot` operation. This is separate from
native Reviews setup and requires exact artifact, target and content approval.

## Optional store reviews

A merchant may add one first-class store subject through normal installation inputs:

```json
{
  "store_review_subject": {
    "subject_id": "merchant-store",
    "path": "/reviews/",
    "name": "Merchant shop"
  },
  "review_invitation_default_checked": false
}
```

The subject is independent of commerce offer IDs and product mappings. Normal provisioning
seeds its existing native `kind=store` row, display name and publication counters in the
same merchant D1. It cannot collide with a product identity/path or be retargeted across
retained releases. The host storefront owns the canonical display page; CartRay does not
require an Astro product/store page generator or a second review application.

For a retained installation, add the input through the existing **OFF**
`reconfigure-catalogue` workflow, then normal build/deploy/verify. First-time store
enablement requires an explicit `review_invitation_default_checked` boolean in that
request (use `false` for an unchecked disclosure); a persisted legacy default is not
treated as an explicit merchant choice. The choice is retained through retries and
compatible rollback. This code change does
not authorise deployment to an existing UAT installation. No migration, new database,
Worker, queue, email provider, activation control or historical enrolment is introduced.

Configured store builds use disclosure `review-invitation-v4`:

> I'm happy to receive invitations to review this shop and the products I purchase.

New store-feature configurations default unchecked unless a merchant explicitly retains
another checkbox preference. Nonconfigured merchants keep their existing product-only
v3 wording and configured checkbox defaults. A cached browser sending only the legacy
boolean retains v1 scope. Existing v1/v2/v3 grants, pending checkout identities and issued
invitation subject sets are never widened by adding this configuration.

For a paid, independently confirmed CartRay order after the existing activation fence,
a newly issued v4 capability includes the store once plus each distinct eligible purchased
product subject. Repeated quantities do not add review slots. A later paid order has its
own store-review slot. Free orders, unpurchased products and external/legacy purchases do
not acquire verified eligibility from configuration alone.

Email permission, suppression and timing still govern solicitation through the one
order-confirmation provider and optional follow-up. Featured/priority selection never
removes other authorised choices from the native review page. A declined v4 checkbox
records that the scope was presented; it is **not** a positive communication grant.
Through an already valid accountless order-access link, a v4 purchaser may deliberately
review even after declining or unsubscribing from requests. This path does not send email,
re-grant notification consent or arm a new follow-up. It still requires the confirmed paid
order, trusted server-side recipient evidence and unrevoked order/capability authority.
Consumed or withdrawn review subjects cannot be submitted again. The same native page
continues to provide existing review management. Resource access survives Reviews failure.

Store submission, moderation, replies, revision history, withdrawal, publication and
recovery use the existing native APIs. Public consumers can read the configured store
subject separately from product subjects. **Do not place store ratings in Product
`aggregateRating` JSON-LD** or combine them with product ratings or unrelated testimonials.
Historical snapshot/privacy approval remains a separate operation; configuring a store
subject does not import or publish historical content.


### Store-review rollout and rollback compatibility

The source server must have a validated store subject before it records v4 scope. An
unexpected v4 request against a product-only server is limited to product-only v3;
checkout can proceed and its original request fingerprint remains pinned. Enabling the
feature later cannot widen that order or reinterpret its pending retry.

Once store reviews are configured or deployed, or any v4 checkout choice exists
(including an unchecked one or a pending order), **schema compatibility alone is
insufficient for application rollback**. The installer checks retained configuration
and actual D1 state before changing configuration or publishing older Workers. An
empty v4 count alone cannot authorise an older target while checkout can still accept v4. It
requires a release marked `store_review_contract: 1` retaining the same configured store
subject. A pre-store release such as `aa0f640` is not a supported backend rollback:
its checkout parser rejects v4, its order-access implementation lacks voluntary v4 access,
and its native invitation reader rejects the 101-subject boundary. This assessment is
source/test evidence, not a hosted test of those old binaries. Small existing capabilities
use an otherwise unchanged subject-kind-agnostic protocol; that is not whole-feature
rollback qualification.

The external host may roll back static frontend assets while retaining the compatible
backend/configuration, or deploy a compatible forward fix. Old cart clients continue
recording their original product-only scope. Never rewind D1, drop grants, regenerate
keys, reopen an uncertain claim or resend history to roll back presentation.

### Refreshing an existing merchant's clients

Use the retained merchant package and existing supported workflow, not a reference
storefront build or manual JavaScript substitutions:

```sh
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" pause
# If first enabling store reviews, reconfigure-catalogue with the retained catalogue/lock/
# mappings plus the optional store_review_subject; preserve the merchant's unchecked setting.
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" \
  build --refresh-clients
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" deploy
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" verify
uv run python modules/reviews/operator/operate.py --directory "$INSTALL" --credentials "$CREDS" doctor
```

`--refresh-clients` copies the complete supported CORE_CLIENT_FILES and REVIEW_CLIENT_FILES
set from this pinned release into a new retained-input snapshot. It preserves the merchant's
commerce catalogue, resource bindings and branding. The store build emits v4 wording/version
and the configured default into `storefront-config.js`; `site-config.js` remains the merchant's
normal theme configuration. The native review client also owns the generic review-region
label, so retained merchant review HTML does not need hand editing for store choices.
The generated gateway must include the static GET/HEAD `/reviews/` pass-through. The host
owns that page's content and deployment. Obtain approved deployment separately; the commands
above describe the supported process rather than authorising changes to any retained UAT.
