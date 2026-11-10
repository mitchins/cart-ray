# Acorn test installation operator

This workflow installs **one** isolated Squirrel/Acorn test store. It uses the
current automatic checkout invitation permission and `reviews_gateway.mjs`.
It does not deploy the older `/review-consent/` bridge, import historical data,
change a theme, rotate suppression keys, or support production. Upgrades and
compatible Worker rollbacks keep the same merchant database and installation
identity. A fresh namespace is for a new installation, not a second version of
an existing store.

## Documented handoff

Start from a reviewed clean Git checkout. Install Node **24** (the repository's
`.nvmrc`), `uv`, and Python supported by `uv.lock`, then run:

```sh
uv sync --locked --all-groups
npm ci --ignore-scripts
node --version
```

The operator supplies two files outside Git. No other private fixture, SQL
snippet, temporary Worker, public activation API, or prior sandbox is required.

1. A private JSON credential file, owned by the invoking user with mode `0600`:

```json
{
  "cloudflare_api_token": "OPERATOR_TOKEN_WITH_ACCOUNT_READ_WORKERS_D1_QUEUES_AND_PAGES_EDIT",
  "stripe_test_key": "sk_test_REPLACE",
  "smtp2go_api_key": "api-REPLACE",
  "approved_test_email": "approved-test-mailbox@example.test",
  "test_sender": "verified-test-sender@example.test"
}
```

The Cloudflare token must be scoped to the supplied test account and support
Workers scripts/version deployments and authenticated remote service bindings,
D1, Queues and Pages. The Stripe key must be a **test** key for the stated
account, with read access to the existing Acorn catalogue and paid test sessions
and permission to create a test webhook endpoint. The SMTP2GO credential and
sender must be authorized for the single approved mailbox. The operator must
also have access to that mailbox to follow the real invitation. Never paste
credential values into commands, logs, PRs or the non-secret inventory.

2. A non-secret install JSON file and the reviewed Acorn Stripe **test preflight
lock** named by `preflight_lock`:

```json
{
  "namespace": "squirrel-ops-test",
  "account_id": "REPLACE_32_CHARACTER_CLOUDFLARE_ACCOUNT_ID",
  "stripe_account_id": "acct_REPLACE",
  "preflight_lock": "/absolute/path/to/stripe-test-preflight.lock.json",
  "merchant_name": "Squirrel test store",
  "merchant_contact": "Squirrel test operations",
  "delay_seconds": 0,
  "max_lateness": 86400
}
```

A namespace must end in `-test`. The lock is produced by the existing
`scripts/preflight_catalogue.py` for `examples/acorn-store/catalogue.csv` and
`fulfilment-expansions.json`; it is not a request to create or alter Stripe
products. The installation copies it into its inputs so subsequent commands do
not depend on the handoff file's original location.

The chosen account needs capacity for **one D1 database, four queues, seven
Workers, two cron triggers and one Pages project**. A quota error is a failed provider operation,
not permission to delete other applications. Resume after capacity is supplied.

Use a scoped durable Cloudflare operator API token for sustained qualification.
A short-lived Wrangler OAuth access-token snapshot expires independently of the
installation: HTTP 401/code 10000 means stop, refresh the authorized operator
session or supply a valid scoped operator token in the protected credential
handoff, then repeat the same command. Never recreate resources or keys to
resolve an expired operator credential.
Existing resources with the same names but without this inventory's creation
journal are rejected.

## Fresh install and resume

Choose an empty private directory outside the repository. Replace the paths in
these commands; keep the same directory for every subsequent command:

```sh
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install plan --config /private/install.json
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json provision
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json migrate
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json build
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json deploy
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json verify
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json doctor
```

`plan` creates no cloud resources or keys. `provision` journals each name before
creating it and saves the returned ID immediately. It generates installation
keys once in `secrets.json`; a repeat/resume never regenerates them. The Stripe
webhook uses a stable idempotency key. If its unknown creation outcome exceeds
Stripe's idempotency window, the operator stops rather than creating another
endpoint. `migrate` installs checksum-ledgered commerce, Reviews and context
schemas and pins the initial suppression identity. It never rewinds a database
or silently adopts a changed/unknown migration. Failed commands retain their
inventory; rerun the same command after resolving the reported failure.

`build` retains clean-commit, locked source and Reviews artifacts and hashes.
`deploy` uploads those artifacts and wires only the inventory's databases,
queues and services. The Pages output uses the current public Reviews gateway;
customer cookies and staff credentials cannot reach public reads. Checkout goes
directly to the separately bound test source origin.

**Verification does not activate the store.** There is no verified runtime
record before explicit activation, so a source wakeup cannot create the
activation fence or backfill earlier purchases. The initial sender is disabled and its processing queues are paused until activation;
the verifier explicitly checks the staged pause.
The `doctor` output distinguishes the staged phase from an activated record.
Private calls use Wrangler's operator-side authenticated remote bindings; no
control Worker or public activation route is deployed.

## Explicit activation

After reviewing the planned resource IDs, approved mailbox and test-only modes:

```sh
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json activate --enable-test-delivery
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json doctor
```

This invokes the existing private activation service, fixes its no-backfill
watermark, records the PR A operator verification, enables only the approved
test sender and bounded producer reconciliation, then relays post-fence events.
A purchase confirmed before this command must remain fenced. Repeating the
command preserves the installation and activation identity. It does not reset
checkpoints, consent, suppression, delivery claims or provider receipts.

`inventory.json` contains names, resource IDs, migration checksums and release
versions; it contains no secret values. Back up the **entire directory**, keeping
`secrets.json` and the credential handoff protected. Losing the original vault
requires restoring its backup. The CLI refuses to generate replacement keys
for retained data or to overwrite a different server-side suppression identity.

## Pause invitation automation after qualification

Stop further automation queue delivery when the approved test is complete:

```sh
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json pause
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json doctor
```

This pauses this installation's events, schedule and due queues. An already
in-flight provider submission may complete; wait for its acceptance receipt
before concluding the test. Checkout and the public unsubscribe route remain
available. Pausing neither cancels an accepted message nor deletes queued hints,
permission/suppression data, provider receipts, keys or the activation fence.
Repeating `pause` is safe. Upgrades and compatible rollbacks retain the paused
intent; only explicit activation resumes it. Keep the installation paused after
qualification.

To intentionally resume, repeat the documented explicit
`activate --enable-test-delivery` command. It verifies the retained installation
before resuming queues and preserves the original no-backfill fence. Retained
post-fence work, including eligible purchases made while paused, can then be
processed; resume only when further test delivery is intended.

Queue messages are expirable hints, not the durable purchase record. The source
relay may continue advancing its checkpoint while queues are paused. The CLI
sets a one-day queue retention period, but pausing may last longer than that:
before any explicit resume it reconstructs signed post-fence event hints from
the durable commerce outbox up to a checkpoint snapshot. Existing subscriber,
scheduler and provider-claim idempotency reconstruct missing schedule/due hints
without resending accepted or uncertain deliveries. Permission, suppression and
maximum-lateness rules still apply; pausing does not extend a customer's policy
window or authorize old pre-fence purchases.

Recovery never rewinds a checkpoint or resets a permission/delivery claim. Each
attempt handles at most twenty pages of twenty hints; if it reaches that budget,
repeat explicit activation within five minutes. Progress is journalled. An old
partial replay is restarted from the original immutable fence, so expired replay
messages cannot authorize resume. Queues remain paused until the whole bounded
snapshot is freshly reconstructed. This also protects interrupted deployments
and rollbacks that outlast queue retention. Preserve the source databases and
private operator inventory for this recovery; deleting them is retirement, not
pause. The operator Queue batch format follows the
[Cloudflare Queue API](https://developers.cloudflare.com/api/resources/queues/subresources/messages/methods/bulk_push/).

## Diagnose, upgrade and application-only rollback

`doctor` reads live bindings, the migration ledgers, suppression identity and
operator record. A wrong binding is reported as, for example,
`producer.bindings.COMMERCE_DB: wrong database`; it is not repaired or marked
healthy. Fix the reviewed configuration/deployment, then verify again. No
migrations, permission grants or delivery operations are performed by doctor.

After checking out a reviewed compatible application commit, run:

```sh
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json upgrade
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json doctor
```

An already activated installation retains its recorded activation authorization
and watermark. Upgrade retains its original keys and databases, validates the
migration prefix, deploys a complete release and refreshes operator verification.
An unactivated installation stays unactivated. Re-run an interrupted operation
from the recorded commit; do not replace its inventory or key vault. The events, schedule and due
queues remain paused across deployment interruptions so an unavailable sender
cannot exhaust delivery retries. Activation or a compatible rollback restores
the sender and verified record before resuming those queues.

```sh
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json rollback --release 0
```

Release indices are the ordered entries in `inventory.json.releases`. Rollback
selects only versions recorded for **this** installation and requires matching
live schema and suppression-key contracts. It changes Worker deployments and
refreshes verification. There is no database restore/down migration, key
replacement or replay of accepted deliveries. An incompatible rollback stops.

## Customer and operator acceptance

Use the canonical storefront URL printed by `doctor`, not a Pages preview URL.
The paid products are the Acorn growing kit and Woodland planter; the free guide
is not reviewable. In the cart, accept the permission checkbox for one purchase,
and explicitly untick it for a second purchase. Pay only through Stripe test
checkout with the approved test email. Verify each stored choice independently:
a preselected acceptance and a declined choice must not be conflated.

For the accepted order, buy **both** paid products in one checkout. Wait for the
single real invitation in the approved mailbox, open its `/review/` link and
submit a review for one offered product. A browser cannot supply payment or
recipient authority. The declined purchase must not receive an invitation.

The operator can inspect pending reviews using the existing private service:

```sh
uv run --locked python modules/reviews/operator/operate.py --directory /private/acorn-install --credentials /private/credentials.json request --path /reviews-admin/v1/pending --admin
```

Save `{"revision_id":"RETURNED_REVISION_ID"}` in `/private/moderation.json`, then
run the same command with `--path /reviews-admin/v1/contributions/RETURNED_REVIEW_ID/approve
--method POST --body /private/moderation.json --admin`. Use the exact returned
revision ID; stale revisions must fail. For rejection/withdrawal replace the final
action with `reject`/`withdraw`. The current staff protocol is also described in
`modules/reviews/console/README.md`; it does not publish a staff route on the storefront.
After approval, reload the mapped product page and its public summary and
contribution list; the other product must not inherit the review.

Follow the invitation email's unsubscribe link: GET/HEAD only show confirmation;
POST records suppression. Make another accepted test purchase using the same
approved mailbox. It must not produce another invitation. Retry/replay must not
resend the earlier accepted delivery. Keep provider acceptance evidence and
assert the single delivery identity, rather than inferring delivery from UI
state alone.

Run the same operator command with `receipt` to save non-secret `receipt.json` in the installation directory. It reads actual paid item counts, accepted/declined checkout choices, the checkpoint, provider acceptance identities, generation states, public counters and suppression counts; it contains no mailbox or invitation capability. Record the clean source commit, deployed versions,
migration/key identity, resource inventory, activation fence, accepted/declined
choice evidence, provider receipt counts, moderation/public presentation,
unsubscribe/later-purchase result, repeat/resume and compatible rollback. Test
checkout independently during a failed Reviews dependency; Reviews unavailability
must not change its successful test checkout response.

The [recorded independent qualification](qualification/2026-11-10.json) distinguishes
the deployed source head from subsequent operator fixes and records the remaining
validation limitations. Detailed resource/provider receipts stay private.

## Shared merchant D1 and optional Reviews

Fresh format-2 installations provision one physical D1 named `<namespace>-commerce`.
The existing `DB`, `CARTRAY_DB`, `COMMERCE_DB`, `REVIEWS_DB` and `CONTEXT_DB`
binding names all reference that database. The deterministic migration order is
commerce, subscriber, Reviews, then purchaser context. The ledger keys each
checksum and position by namespace; paths remain globally unique. A changed,
unknown, missing or reordered ledger fails closed. Repeat `migrate` to resume;
never edit the ledger to bypass a failure. Failed Wrangler migration output is
retained in `migration-error.log` with mode 0600 in the private installation
directory; the error identifies that file and the retry command.

Set `"reviews_enabled": false` in the initial installation JSON for commerce
only. This provisions one source Worker, one D1 and one Pages project, applies
only commerce migrations, and omits Reviews services, queues, builds, storefront
gateways and mail credentials. The default remains Reviews enabled. This choice
is fixed by the retained inventory; create a fresh installation to change it.
Commerce activation requires a completed deployment but no delivery opt-in.
Pause and resume retain checkout availability; there are no delivery queues.

D1 bindings grant access to the whole database: separate binding names are not
table-level isolation. Reviews and purchaser-authority Workers therefore have
wider database access in this topology. Service authentication, independent
installation identity, signed attestation scope, pinned versions, activation
fences and suppression key checks remain mandatory. A D1 outage affects both
checkout and Reviews; neither is an independent database failover boundary.

This installer accepts only new format-2 inventories. It does not move or merge
existing three-database installations, rewrite their schema history, or offer an
automatic upgrade of old operator inventories. Preserve existing inventories,
keys and databases and use a fresh namespace for this topology.

The [independent INFRA-01 hosted qualification](qualification/infra-01-2026-11-10.json)
used clean, frozen runtime source `4fd56d7a3a79de2b9c2b82ccd95c3e23ade2e8aa`
with no implementation or credential intervention during installation. Physical
D1 count fell from three to one; seven Workers, four queues and one Pages project
were unchanged. Live inspection confirmed all seven D1 binding aliases targeted
the same physical database. The final installation was healthy and paused after
checkout, delivery, browser submission, moderation, suppression, Reviews outage,
drift diagnosis, recovery and compatible same-source rollback checks.

Measured doctor time was 47.293 seconds on the prior topology and 15.942 seconds
on the shared topology. Median public summary time was 1.133 versus 0.223 seconds
and storefront time was 0.171 versus 0.168 seconds (three samples each). These
are observations with network, cache and warm-state differences, not evidence
that D1 consolidation alone caused the change. Commerce-only mode was qualified
locally, not hosted; the evidence records the remaining scope limitations.
