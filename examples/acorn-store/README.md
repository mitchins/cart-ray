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

## Review invitation permission

The test store config selects automatic invitations. When test checkout is
configured, the cart displays an initially unchecked checkbox asking permission
for one post-purchase review email. The choice travels with checkout; the
server records its own wording version and binds any grant to the created
order. The browser does not supply a recipient or establish payment authority.
The separately deployed CartRay test Worker must also opt in with
`CARTRAY_REVIEW_INVITATION_MODE = "automatic"` in its `[vars]`; see
`cartray-test-vars.example.toml`. Without that trusted server setting, even a
forged browser request cannot record a grant. The storefront setting alone
never enables the feature.

The default preview has checkout and the checkbox disabled. This configuration
alone does not deliver email or activate collection. A confirmed purchase,
trusted recipient and a qualified delivery pipeline are still required before
sending. Receipt-only invitations are a future option and are not implemented
by this example.
