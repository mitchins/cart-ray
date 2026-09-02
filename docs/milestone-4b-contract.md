# Milestone 4b contract

## Scope

M4b turns the merged M4a settlement kernel into a test-only operational proof. It deploys the
existing Worker and Pages storefront, applies the two reviewed D1 migrations, configures the
minimum Worker bindings, registers one Stripe sandbox webhook destination, and records acceptance
evidence. It does not enable Stripe live mode, migrate a real catalogue, invoke Valet, add a Queue
consumer, or make CartRay a production service.

## Deployment boundary

Every remote Wrangler command must select Cloudflare account
`33623e6d0b4793842a1832c5512aeb49`; another account is available to the local login. The Worker
uses the existing test-only D1 database binding `DB` (`cartray-test`). Before the first deployment,
apply migrations by binding name with:

```sh
CLOUDFLARE_ACCOUNT_ID=33623e6d0b4793842a1832c5512aeb49 \
  nvm exec 24.20.0 uv run pywrangler d1 migrations apply DB --remote --config wrangler.toml
```

The named Worker configuration declares the four sensitive bindings as required secrets. They are
never committed, printed, passed on a command line, or put in a Wrangler `vars` section.

| Secret | Required value |
| --- | --- |
| `STRIPE_API_KEY` | Test-only `rk_test_…` preferred (or `sk_test_…`); it must read Prices and Checkout Sessions and write Checkout Sessions and Payment Intents. |
| `CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64` | A new Ed25519 private key: DER PKCS#8, standard base64. |
| `CARTRAY_PROJECTION_PUBLIC_KEYS_JSON` | JSON object mapping the active key ID to its Ed25519 SPKI public key in standard base64. |
| `STRIPE_WEBHOOK_SECRET` | The `whsec_…` secret of this deployed test webhook destination only. |

Because required secrets are checked before the first deployment and `wrangler secret put` cannot
populate a Worker that does not yet exist, the first deployment uses a user-created, gitignored
`.m4b-bootstrap.env` file supplied to the deploy command. It contains the first three real values
above and a locally generated high-entropy `STRIPE_WEBHOOK_SECRET` bootstrap value. Create the file
with owner-only permissions, never use a documented or predictable placeholder, and do not reveal
the value. Delete the file immediately after the successful bootstrap deploy. After the hosted URL
exists and Stripe creates the destination, replace only the remote bootstrap value through
Wrangler's interactive secret prompt. The file is never committed or sent through a command
argument.

```text
STRIPE_API_KEY=<test-only restricted key>
CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64=<new standard-base64 PKCS#8 private key>
CARTRAY_PROJECTION_PUBLIC_KEYS_JSON={"cartray-test-2026-09-01":"<standard-base64 SPKI public key>"}
STRIPE_WEBHOOK_SECRET=<locally-generated-high-entropy-secret>
```

For example, first set `umask 077`, create the file, generate a fresh value with a local CSPRNG,
and paste that value into the final field without displaying it in chat, source control, or command
history. Confirm the file mode is `600` before deployment.

Run the protected bootstrap only with the pinned local runtime:

```sh
CLOUDFLARE_ACCOUNT_ID=33623e6d0b4793842a1832c5512aeb49 \
  nvm exec 24.20.0 uv run pywrangler deploy --config wrangler.toml \
  --secrets-file .m4b-bootstrap.env
```

The non-secret `CARTRAY_SIGNING_KEY_ID` is `cartray-test-2026-09-01` and must exactly equal the sole
active JSON key. The public test storefront origin is `https://cartray-store-test.pages.dev`; its
success and cancellation redirects return to that same origin with the `checkout` query value. The
Pages production build must point only to the resulting Workers.dev HTTPS URL.

`CHECKOUT_RATE_LIMITER` is a test-only route-level Cloudflare Rate Limiting binding: 20 checkout
attempts per 60 seconds per Cloudflare location. It uses the fixed `cartray-test:checkout` key and
is an abuse bound, not user authentication, financial accounting, or a cross-location guarantee.
The Worker fails closed when the binding is unavailable.

## Stripe sandbox activation

Create a Stripe destination for events on the account, targeting:

```text
https://<Workers.dev-host>/stripe/webhook
```

Subscribe only to `checkout.session.completed`. Its event data is test mode only. Add the generated
endpoint signing secret as `STRIPE_WEBHOOK_SECRET` after the endpoint exists. The raw request body
and that exact destination secret are required for signature verification; a Stripe CLI listener
secret is not interchangeable with a Dashboard/Workbench destination secret.

CartRay pins both its outgoing Stripe API calls and its sandbox destination to
`2025-09-30.clover`. This is the current, sandbox-proven integration contract; do not inherit the
account default or configure a separately older endpoint version. This is a destination-level test
configuration change, not an account-wide Stripe API upgrade.

```sh
CLOUDFLARE_ACCOUNT_ID=33623e6d0b4793842a1832c5512aeb49 \
  nvm exec 24.20.0 uv run pywrangler secret put STRIPE_WEBHOOK_SECRET --config wrangler.toml
```

Enter the destination's revealed `whsec_…` value only at the interactive prompt.

## Acceptance evidence

After deployment, prove with genuine Stripe sandbox Checkouts:

1. one paid order;
2. one native free order;
3. one paid catalogue item reduced to zero by the approved `CRTEST100` coupon;
4. one quantity-five support-hours order;
5. one resent genuine `checkout.session.completed` delivery.

For every order, retain only IDs and non-secret operational facts showing: a completed Stripe Session,
matching D1 Session/order IDs, `settlement_state=confirmed`, a confirmed Stripe event record, and
exactly one `OrderConfirmed` outbox row. The resent event must return HTTP 200 without increasing
the confirmation or outbox counts. Check `/health`, `/catalogue`, exact-origin CORS, the checkout
rate limit, and the storefront checkout redirect as separate observations.

## Exit criteria

M4b is complete only when remote migration status is clean; Worker secret listing exposes names but
no values; the test-only Worker/Pages deployment is live; all acceptance evidence above is recorded;
and an adversarial exit review finds no remaining operational defect. Deployment can be paused or
rolled back by user direction; this milestone does not authorize live-mode promotion.
