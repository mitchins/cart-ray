# Squirrel’s Acorn Store — greenfield reference (test only)

This example builds an independent CartRay storefront from its own catalogue.
The fixture catalogue contains two paid products and one free, non-reviewable
guide. `store.json` is the explicit mapping from stable CartRay product keys to
canonical paths and Reviews subject IDs. The three fixture prices and assets are
synthetic and are not Stripe objects. There is no WordPress or Effective Policy
dependency.

## Build and preview

From the repository root with the locked `uv` dependencies:

```sh
uv run --locked python examples/acorn-store/build.py --output /tmp/acorn-store-preview
uv run --locked python -m http.server 8788 --directory /tmp/acorn-store-preview
```

Open `http://127.0.0.1:8788/`. This default build has checkout and Reviews
reads **disabled**. Product pages use the existing CartRay cart and checkout
client; nothing is charged or sent. The output directory must be new so a build
cannot silently leave stale assets behind.

To compile the *commerce Worker* against these reviewed synthetic catalogue
inputs, select `CARTRAY_CATALOGUE_PROFILE=acorn-test` when running the existing
locked vendoring/synchronization workflow. This is an opt-in build input, never
a mutation of the default catalogue. The preview price IDs are placeholders:
they cannot be used as proof of real Stripe test checkout. Before connecting a
test Worker, a merchant must create and independently preflight Stripe **test**
prices for the three lookup keys. Never add secret keys or real customer data to
this example or build output.

To prepare an actual test checkout, create active, fixed, one-time Stripe
**test-mode** prices for `cr_test_acorn_kit` (AUD 24.00),
`cr_test_acorn_planter` (AUD 18.00) and `cr_test_acorn_guide` (AUD 0.00).
No Stripe resources are created by this example. With a read-only `rk_test_`
or `sk_test_` key supplied securely as `STRIPE_API_KEY` in the environment, run
the existing read-only preflight:

```sh
uv run --locked python scripts/preflight_catalogue.py \
  --catalogue examples/acorn-store/catalogue.csv \
  --fulfilment-expansions examples/acorn-store/fulfilment-expansions.json \
  --output-lock examples/acorn-store/.private/stripe-test-preflight.lock.json
```

The ignored private lock binds the exact test Price identities and catalogue
version to the `acorn-stripe-test` Worker build profile. Select that profile
with `CARTRAY_CATALOGUE_PROFILE=acorn-stripe-test` for the existing Worker
vendoring build. Missing, stale, live-mode or mismatched locks fail closed.
The lock contains test product/price metadata and must not be committed.
Use a **separate test-only** Worker, D1 database, signing keys, test Stripe
webhook secret and storefront origin; the repository's existing Wrangler
configuration points at another deployment and must not be reused unchanged.
Provisioning those resources and configuring a test webhook are separate steps.

After preflight, an explicit `--test-checkout-api https://TEST-WORKER.example` builds the site
against the separately provisioned CartRay **test-only** checkout Worker. The
build checks that the local test lock matches the catalogue and reference prices. The URL
is operator-supplied: the build cannot attest the remote Worker's Stripe mode.
Verify its test-environment and test-key guards and the reviewed price preflight
and that its build used the same lock before enabling it. No remote Worker, Stripe object or D1 database is created by
this build.

## Optional Reviews

`--reviews` enables public read-only rendering on the two mapped product pages.
A same-origin test gateway must route only `/reviews/v1/` to a separately
installed Reviews Worker scoped to `acorn-store` / `test`; a missing or stale
projection displays an unavailable state. The free guide never requests reviews.
Review text is rendered as text, not HTML. A version mismatch clears the list;
rated and unrated contributions stay separate and replies remain attached.
No structured data is emitted until versioned publication and withdrawal are
qualified for the actual storefront. Existing R5 publication, CDN invalidation,
and production freshness guarantees are not replaced by this development view.

The output includes the existing test invitation and management forms at
`/review/`; they require the same-origin Reviews proxy and a valid purchase-bound
test invitation. Merely opening a page creates no invitation. The separate
staff console in `modules/reviews/console` must be served from its **own**
authenticated staff origin/gateway. The public build does not contain the
console and must never route `/reviews-admin/` to the public storefront.

## Remaining private-beta work

This example is a runnable preview and an integration contract, not the Squirrel
test acceptance or a production deployment. Actual subject installation,
independently authenticated staff, real Stripe test checkout, trusted purchaser
and consent evidence, scheduled delivery, provider receipts, a reviewed public
projection and hosted withdrawal/performance qualification remain to be wired
and demonstrated. Do not enable real collection, live billing or outbound
campaigns. The production configuration and cache/withdrawal policy require
separate qualification; do not publish historical or customer data through this
preview.

## Optional review-request consent bridge

The `--review-consent` build flag exposes an optional review-request choice only
when an independently authenticated consent authority reports a verified
purchase context. It also requires the explicit test checkout build and a
same-origin gateway for `/review-consent/v1/`. The default build never displays
the choice. Checkout does not call this gateway and continues when it is absent.

`modules/reviews/consent/capture.mjs` is the test-only bridge: it validates
origin, request shape and rate limits (IPv6 /64 and IPv4-mapped addresses use
the same admission buckets, keyed with a dedicated secret), forwards the authenticated browser cookie
only to a private service binding, and signs the returned purpose-specific
consent evidence for the independent Reviews service binding. The browser never
supplies an event, recipient, store or evidence identity. The authority must
verify the purchaser against the authoritative acquisition, capture the
explicit opt-in independently of settlement, persist a stable evidence ID and
grant timestamp before replying, and return the same evidence on retries. It
must not treat a checkout session ID alone as purchaser authentication. The
bridge deliberately fails closed if any required binding is absent.

**No production or reference implementation of that authority is supplied.**
The flag alone cannot capture usable consent or verify a recipient; it must stay
disabled until the authority, cookie/session security and trusted bindings have
been implemented and independently qualified. This bridge does not send email,
activate collection, or subscribe to purchase events.
