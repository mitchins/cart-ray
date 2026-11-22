# Effective Policy catalogue UAT

This is a local, no-index UAT audit of the approved public/demo handoff, not a production shop, delivery system or WordPress migration. It displays all published entries, including source-hidden entries marked as such, for audit; it must not be published as the merchant's production catalogue. It uses the existing GEL theme assets, shared configurable header and gallery code, but the UAT page builder is not yet a supported generic merchant storefront or installer.

The source archive is intentionally not in this repository. Prepare a snapshot with `uv run --locked python examples/ep-uat/prepare.py --help` and build it with:

```sh
uv run --locked python examples/ep-uat/build.py /path/to/prepared /path/to/new-output --theme minimal
```

Source-description links are disabled by default. To include reviewed links, pass
the same `--link-policy examples/ep-uat/approved-link-policy.json` to **both**
`prepare.py` and `build.py`. The policy currently approves only the captured
quote link and its verified same-origin destination. Other links stay disabled.
Adding a mapping requires review of its source and destination, including whether
the destination is non-transactional; syntax checks cannot determine what a
remote page does. Policies can express explicit safe HTTPS, same-origin paths or
`mailto:` links, but do not themselves send email or perform transactions. The
build rejects markup containing a destination absent from its supplied policy.

Select `minimal`, `studio` or `classic`. The default EP UAT GEL is `gel.json`; supply a merchant-specific validated manifest with `--gel`. Its identity and currency must match the catalogue, and only the approved source logo may be used. Colours are provisional UAT choices. The builder validates source image fingerprints and copies only declared approved media. The local routes are UAT routes, not source canonical URLs. Browse-only products remain visible without payment or entitlement mappings. Variations are selectable under their source parent; they are not checkout-authorised.

## Controlled sandbox lane

`provision_test_prices.py --help` explains the opt-in sandbox fixture: the approved source archive is checked again, selected source prices are explicitly mapped to Stripe test Prices, and dummy fulfilment is separate from source catalogue authority. Keep the generated fixture and test key private. Never use a live key, real paid files, or real customer data. A single test-only transactional email may be explicitly enabled after a confirmed purchase to the approved test mailbox; it contains only the supplied dummy Dropbox transfer.

Build with `--test-fixture /private/fixture/test-price-map.json`, then run `test_server.py --help`. The server binds loopback only, uses real Stripe test mode and CartRay's existing checkout/settlement code, and grants a harmless dummy file to the same browser context. It is a local qualification harness backed by SQLite, not a production download service, a supported merchant deployment, or evidence of cross-device delivery. Checkout remains disabled when the test server is absent. No production consent or Reviews automation is enabled.

To run browsing checks, install the matching Playwright browser versions and use `browser_qualification.py --help`. Scaling snapshots made by `scale_fixture.py` are explicitly synthetic and are not source data.

The browse snapshot distinguishes listing identity and presentation from checkout authority. Each listing must carry an explicit local `navigation_path`, source identity, categories, images, display-only price, product action and parented variation records. Navigation paths are validated and unique; they are not canonical URL assertions. Source currency comes from the catalogue and must match the supplied GEL. The EP adapter supplies its own local audit paths; snapshots without paths must be updated explicitly. Checkout-authorised offers remain a separate, explicitly scoped test fixture. This makes the browse input reusable, but the UAT builder and loopback checkout remain qualification tools, not a supported generic merchant installer.

To recheck source media formats, run `uv run --locked --with "pillow==13.0.0" python examples/ep-uat/media_qualification.py /path/to/prepared --output /path/to/media-audit.json`. The check decodes raster files and checks the free PDF signature; it is not a PDF content audit.

The malformed-media regression runs with the same optional tool dependency: `uv run --locked --with "pillow==13.0.0" pytest -q tests/ep_uat/test_media_qualification.py`. The production CartRay runtime does not depend on Pillow.

An explicit `--review-fixture` can map selected source IDs to synthetic Reviews subjects. Such builds include the existing native review and management clients and the shared theme presenter; unconfigured builds omit those routes. The pages require an independently configured same-origin Reviews gateway and a valid purchase-scoped capability. A mapping alone does not create purchase authority, publish reviews, send invitations or make the UAT checkout a production integration.

For a local synthetic review journey, create a test-only JSON fixture with `schema_version: 1`, the prepared `source_archive_sha256`, and a `subjects` object mapping source ID `171` to `example-policy`. Build with `--review-fixture /path/to/fixture.json`, install the repository's locked Node dependencies, then run:

```sh
uv run --locked --with 'playwright==1.57.0' python examples/ep-uat/review_customer_qualification.py \
  --site /path/to/built-site --source /path/to/prepared --output /path/to/new-evidence
```

The test uses disposable local D1 data and a synthetic confirmed acquisition, not the real Stripe test order. The default native acquisition suite remains separately runnable with `uv run --locked python modules/reviews/tests/native_acquisition_workerd.py`; the browser scenario intentionally uses only the part of that setup needed to create the synthetic invitation.

## Test-only transactional delivery

Pass `--smtp2go-key-file /private/path/to/key` to the loopback server to enable delivery from `uat@mitchellcurrie.com` to `test-1034@mitchellcurrie.com` only. The server retrieves the Checkout Session directly from Stripe, checks the test-mode paid state and the Stripe purchase contact, and requires the previously reconciled confirmed order and `OrderConfirmed` event. The original harness reconciled on browser return; the optional local scheduled runner below is intended to qualify the no-return case. A persistent claim per order and workflow is committed before sending; matching multiple workflows sends one test email per workflow. A timeout or crash is held for investigation and is never blindly resent. Checkout remains independent of the mail provider. The normal server remains email-disabled if this flag is absent. The provider accepting a message does not prove mailbox delivery.

This is a local UAT implementation, not production delivery infrastructure or a customer recovery portal. It sends the source-approved dummy links only. Test links may expire under the provider's own rules; verify real fulfilment link lifetime and production sender/recipient mapping separately before cutover.

### Persistent test credentials on macOS

Keep the Stripe sandbox and SMTP2GO test credentials in the operator's macOS login Keychain, using separate generic-password entries named `cartray-ep-uat-stripe-test` and `cartray-ep-uat-smtp2go-test`. Create or update those entries interactively in Keychain Access; do not put secrets in command arguments, shell history, source control, the site output, or a disposable worktree. Use `--stripe-keychain-service cartray-ep-uat-stripe-test --smtp2go-keychain-service cartray-ep-uat-smtp2go-test` when starting the local server. Keychain may request access approval. The credential values are read into the local process only and the server never deletes the entries. Existing `--key-file` and `--smtp2go-key-file` options remain available for protected operator-managed files. Revoke or rotate credentials only when the operator chooses or an actual exposure requires it.

### Local post-purchase event experiment

The UAT sender can use the shared `cartray.post_purchase` dispatcher. Each action has a workflow key, offer match, registered handler, configuration reference and SHA-256 digest of its resolved immutable configuration. It consumes only orders already confirmed with an `OrderConfirmed` outbox record and keeps separate per-merchant/environment/order/workflow execution claims. It does not modify the shared outbox delivery marker. The UAT adapter hashes its sender, recipient, email subject, body and dummy links; any change to those facts or the offer match is rejected for an existing execution. A claim is durable before a handler is called; an uncertain outcome requires review rather than an automatic resend.

For local testing only, enabling the SMTP2GO test key starts a bounded, periodic SQLite reconciliation/dispatch runner. It uses existing CartRay reconciliation and can confirm a test purchase even if the browser never returns. `--no-return-test` directs newly created test Checkouts to a separate local neutral page after payment, leaving the store's return handler unused. The runner and SQLite dispatcher are qualification tools. The D1 dispatcher and drain callable passed a local Workerd smoke; the default-off webhook/scheduled wakeups have Python tests. Actual Workerd triggers, deployment, trusted recipient/provider handler and production operation remain unqualified. The recipient is fixed to the approved test mailbox and no production messages are sent.

### Generic Worker boundary (test only)

The CartRay Worker has an optional `CARTRAY_POST_PURCHASE_MODE=test` path. When explicitly enabled in a test environment, a successful Stripe webhook or scheduled reconciliation wakes a bounded drain of the existing `OrderConfirmed` outbox. The drain requires an independently provisioned checkpoint and installation scope, plus `CARTRAY_POST_PURCHASE_CONFIG`, a private `POST_PURCHASE_HANDLER` service binding and a separate service token. Missing or invalid configuration is fail-closed. The handler receives the trusted order/session/items and workflow identity; it must independently resolve the purchase recipient, enforce the pinned configuration digest, and return a provider receipt. The core does not trust browser recipients or execute arbitrary browser scripts. Uncertain sends remain held.

`modules/post-purchase/transactional-email/worker.mjs` supplies a test-only private handler for that binding. It is disabled unless `DELIVERY_MODE` and `ENVIRONMENT` both equal `test`, and requires the caller's separate `HANDLER_TOKEN`. Operator-provisioned `DELIVERY_CONFIG` is a JSON object with an `actions` array; each action has `environment`, `merchant`, `workflow_key`, `config_ref`, `offer_keys`, `links`, `recipient`, `sender`, `subject` and `stripe_account`. Set the corresponding core action's `config_digest` to the SHA-256 of the UTF-8 bytes of JavaScript `JSON.stringify(action)`; action identity and digest must match. `APPROVED_TEST_RECIPIENT`, `APPROVED_TEST_SENDER`, `STRIPE_ACCOUNT_ID`, `STRIPE_API_KEY` and `SMTP2GO_API_KEY` are separate private bindings. Configure exactly one approved test sender and recipient. This adapter accepts only dummy Dropbox transfer links, checks the actual Stripe test account and paid session, and returns provider acceptance rather than claiming mailbox delivery. Never expose the handler as a public route.

Each webhook or scheduled drain examines at most ten events, starts at most two new handlers, and has a 22-second execution budget. It defers a new claim unless at least thirteen seconds of the overall budget remain, allowing for the eight-second handler wait, two-second response parsing limit and a database margin. The private handler shares a seven-second deadline across Stripe and SMTP2GO; a timeout is an uncertain outcome, never permission to resend. On cancellation the dispatcher attempts to hold a claimed execution as uncertain; an interrupted database operation can still leave a claim pending, and its checkpoint remains at that event until the claim has a recorded outcome. Future scheduled invocations continue bounded batches.

### Recover an interrupted test delivery

The test-only operator CLI uses the operator's existing Cloudflare credentials and a named test D1; it is not a public endpoint. First pause **both** webhook-triggered and scheduled post-purchase processing by deploying the source Worker with `CARTRAY_POST_PURCHASE_MODE` set to `disabled`, and ensure the prior in-flight work has stopped. Wait at least 20 minutes after the claim's last update; the database enforces this minimum. A claim's age does not establish whether mail was sent. Inspect the provider's authoritative history and retain its receipt or record that the outcome remains unknown. Do not put addresses, tokens or other secrets in the audit note.

```sh
uv run --locked python scripts/post_purchase_recovery.py inspect \
  --account ACCOUNT_ID --database MERCHANT-test --merchant MERCHANT_ID
uv run --locked python scripts/post_purchase_recovery.py hold \
  --account ACCOUNT_ID --database MERCHANT-test --merchant MERCHANT_ID \
  --order ORDER_ID --workflow WORKFLOW_KEY --digest INSPECTED_ACTION_DIGEST \
  --operator OPERATOR_ID --evidence 'provider history checked; outcome unknown' \
  --confirm-worker-paused
```

Inspection lists claimed executions first, then uncertain executions, 100 at a time; use `--offset 100` for the next page. The hold atomically records an immutable audit entry and changes only the matching old `claimed` execution to `uncertain`. It never deletes or retries the claim, assumes delivery failure, or rewinds the checkpoint. Accepted and already uncertain outcomes cannot be held again, and a changed action digest cannot be substituted. After reviewing the recorded result, restore the permitted test configuration; the next scheduled drain can progress to later purchases. A delayed or ambiguous provider outcome remains an operator matter. Production use is not enabled by this command.

The CartRay dispatcher owns the durable `(merchant, order, workflow)` claim. The handler is stateless and is not safe to call or retry independently: timeouts and unknown provider outcomes stay held for operator reconciliation. This remains a test adapter; a supported installation and hosted event-to-email qualification are not established by its presence.

An operator-facing activation/install workflow, deployed handler, recipient lookup and real provider integration for this boundary are not yet qualified. The local smoke uses a synthetic in-process service, not an independently deployed service binding. Production use remains disabled.

### Offline parity and head composition

The same UAT product and catalogue renderer can create an **offline-only,
noindex** candidate using `--offline-seo /absolute/path/seo.json`. The JSON has
`schema_version: 1`, an explicit same-origin `catalogue_path`, and a `pages`
object mapping each product navigation path and the catalogue path to the shared
page SEO object. The canonical URL in each object must match its path; all
canonical origins must match. Missing routes fail before output is created.
Metadata comes from an independently approved source; the builder does not infer
canonical URLs from product keys, source IDs or UAT paths. Descriptions are
preserved, including absence. Product-only structured data is restricted to the
CartRay allowlist; a host that owns the HTML head can supply its independently
verified richer graph separately.

This mode forces `noindex,nofollow`, removes UAT notices and test-cart fixture
configuration, and rejects `--test-fixture` and `--review-fixture`. Product
purchase controls are represented by a `data-cartray-purchase-action` placeholder
for the host's supported commerce integration; they are not functional checkout
or fulfilment authority. This is a renderer and metadata qualification artifact,
not a deployable production approval. Default UAT output remains noindex and does
not emit canonical tags.
