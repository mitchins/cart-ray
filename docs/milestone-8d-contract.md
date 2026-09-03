# Milestone 8d contract: locked real test-subset deployment

## Scope

M8d makes the already reviewed `real-test-subset` available to the existing CartRay **test**
Worker and Pages build. The test deployment contains exactly the committed real-subset CSV,
presentation sidecar, local assets, private fulfilment expansions, and Stripe test preflight lock.

It does not change live mode, Woo, the Stripe test Products or Prices, D1 schema, webhook
configuration, fulfilment delivery, Valet, or the checked-in synthetic development catalogue.

## Closed build profiles

`CARTRAY_CATALOGUE_PROFILE` is a build-only selector with exactly two values:

| Value | Inputs | Normal use |
| --- | --- | --- |
| `synthetic` (default) | existing `fixtures/` rows and fixture Price resolutions | local preview, CI, and the checked-in generated Worker module |
| `real-test-subset` | `catalogue/real-test-subset/` plus `stripe-test-preflight.lock.json` | explicit sandbox Worker/Pages deployment only |

The selector never accepts a path, URL, SKU, browser value, or Stripe response. A non-listed
value fails before an artifact is produced. The real profile is compiled into Pywrangler's copied
Worker source only; it does not replace `src/cartray/compiled_catalogue.py`. This preserves the
synthetic default for normal development and ensures the real deploy is an explicit operator act.

The Pages production build validates the same selected profile and its local WebP assets before it
uploads static files. Pages continues to fetch its public catalogue from the Worker; it does not
receive Stripe Price IDs or private fulfilment expansions.

## Sandbox operator sequence

After this implementation is merged, use the incumbent Cloudflare OAuth-backed Wrangler session;
do not set the D1-scoped `CLOUDFLARE_API_TOKEN` for these deployment commands.

```sh
cd /Users/mitchellcurrie/Projects/cart-ray
source /Users/mitchellcurrie/.nvm/nvm.sh
nvm use 24.20.0
unset CLOUDFLARE_API_TOKEN

CARTRAY_CATALOGUE_PROFILE=real-test-subset \
uv run pywrangler deploy --config wrangler.toml

CARTRAY_CATALOGUE_PROFILE=real-test-subset \
CARTRAY_STOREFRONT_MODE=production \
CARTRAY_STOREFRONT_API_BASE_URL=https://cartray-test.mitch-336.workers.dev \
npm run build:storefront

uv run pywrangler pages deploy storefront-dist \
  --project-name cartray-store-test \
  --branch main
```

The operator then completes exactly one test-mode paid `EP-SIL-2026` Checkout and one native-free
`EP-LMS-TRAINING-CATALOGUE` Checkout. M8e will record sanitized Stripe and D1 acceptance evidence:
the real catalogue version, each settlement state, immutable item snapshot, and exactly one
`OrderConfirmed` row per order.
