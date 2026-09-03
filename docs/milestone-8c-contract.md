# Milestone 8c contract: real test-subset source inputs

## Scope

M8c supplies the reviewed local inputs for the first two real CartRay products. They are not
connected to the currently deployed synthetic catalogue, nor to live Stripe, Woo, Dropbox, or
Valet.

| CartRay product key | Woo reference | Test Price lookup key | Amount |
| --- | --- | --- | --- |
| `EP-SIL-2026` | `sil-package-1` / Module 5a - Supported Independent Living Policy and Procedure Package | `cr_test_ep_sil_2026` | AUD 850.00 |
| `EP-LMS-TRAINING-CATALOGUE` | `lms1` / LMS Training Catalogue | `cr_test_ep_lms_training_catalogue` | AUD 0.00 |

Each is a singleton download in this test subset. Their private fulfilment expansion initially
equals the canonical CartRay product key. That is a deliberate, source-neutral provisional
entitlement identifier; a later Valet integration can map it to media or download permissions
without changing the Stripe-facing CartRay key.

## Local presentation assets

The two files in `storefront/assets/products/` are local WebP derivatives of the corresponding
Effective Policy Woo artwork. Their inclusion was explicitly approved by the repository owner for
this FOSS test subset. They are static build inputs, not remote image URLs. Replacements must use a
new versioned image key rather than mutating an existing asset.

The descriptions are short, plain-text storefront summaries. They are intentionally not a copy of
the Woo HTML descriptions and cannot carry HTML or Markdown into the browser.

## Stripe test-mode operator step

Create the following two Stripe **test-mode only** Products and active, fixed, one-time Prices:

| Product | Currency | Unit amount | Lookup key |
| --- | --- | --- | --- |
| Module 5a - Supported Independent Living Policy and Procedure Package | AUD | 850.00 | `cr_test_ep_sil_2026` |
| LMS Training Catalogue | AUD | 0.00 | `cr_test_ep_lms_training_catalogue` |

Do not create recurring, custom-amount, inactive, or live-mode Prices. Stripe product images and
descriptions are optional: CartRay renders its reviewed local presentation sidecar, not Stripe
product content.

After creation, run the read-only test preflight:

```sh
uv run python scripts/preflight_catalogue.py \
  --catalogue catalogue/real-test-subset/catalogue.csv \
  --fulfilment-expansions catalogue/real-test-subset/fulfilment-expansions.json \
  --output-lock catalogue/real-test-subset/stripe-test-preflight.lock.json
```

The resulting lock must be reviewed and committed before the compiler may build a real subset.
M8d will use that lock to compile, deploy, and test one paid and one native-free Checkout.
