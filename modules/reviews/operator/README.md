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
and one approved test recipient. For `post_purchase_email_provider: "cloudflare"`, supply
`cloudflare_email_api_token` instead of `smtp2go_api_key`. There is one selected provider
for receipts, optional review follow-ups and customer-requested recovery.

Use schema 2 for the protected `post_purchase_config` file:

```json
{"schema":2,"sender":"verified@example.test","recipient":"approved@example.test"}
```

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

## Evidence and limits

See `docs/order-reviews.md` and the checked-in qualification receipts for the exact tested build.
The test adapter remains recipient-restricted. Native staff moderation and capability APIs remain
private; the storefront gateway cannot expose administrative APIs. Shared D1 means Workers with a
binding have database-wide access, not physical table isolation. A complete D1 outage affects
Commerce and Reviews; an unavailable optional Reviews service does not block checkout or receipt.
