# Milestone 8b contract: preflight-locked controlled real subset

## Scope

M8b adds the private lock that joins an explicitly reviewed local catalogue subset to its resolved
**Stripe test-mode** Prices. It is the boundary needed before CartRay can compile or deploy a real
test subset without accidentally substituting the synthetic fixture Prices.

The approved subset is deliberately limited to:

| CartRay product key | Current Woo SKU | Current Woo product | Test mode |
| --- | --- | --- | --- |
| `EP-SIL-2026` | `sil-package-1` | Module 5a – Supported Independent Living Policy and Procedure Package | paid |
| `EP-LMS-TRAINING-CATALOGUE` | `lms1` | LMS Training Catalogue | native free |

These CartRay product keys are source-neutral. Woo SKUs remain migration-reference data; the
browser and Stripe metadata carry only CartRay product keys.

M8b does not alter Woo, create Stripe objects, import historical customers, change live mode,
grant Valet entitlements, or replace any current synthetic deployment. It also does not copy Woo
artwork into this FOSS repository. The real subset needs deliberately licensed or user-approved
local WebP assets before it can be published.

## Preflight lock

The operator runs the existing read-only test-key preflight against the reviewed local CSV and
private fulfilment expansions, adding `--output-lock`:

```sh
uv run python scripts/preflight_catalogue.py \
  --catalogue <real-subset-catalogue.csv> \
  --fulfilment-expansions <real-subset-fulfilment-expansions.json> \
  --output-lock <real-subset-preflight.lock.json>
```

The resulting private lock has schema `1` and records only:

- the CartRay catalogue digest;
- the exact Stripe API version and `test` mode marker; and
- each resolved lookup key, Price ID, amount, and currency.

It contains neither credentials nor fulfilment resources. The normal sanitized preflight JSON on
stdout remains public-safe and excludes Price IDs.

The compiler accepts either the synthetic `--price-resolutions` fixture input or a real
`--preflight-lock`, never both. A lock compiles only if the local source rows, private expansions,
and resolved prices reproduce its exact catalogue digest. A compiled lock is enforced again when
the Worker resolves Prices at runtime; changed Price IDs, amounts, currencies, or source semantics
fail closed before a checkout can be created.

## Operator inputs still required

Before the subset is compiled, an operator must explicitly review and add:

1. the two local CSV rows using the keys above and test-only lookup keys;
2. their private fulfilment expansions, using the future Valet entitlement identifiers—not
   browser or Woo-authored values;
3. test-mode Stripe Products and one-time, active Prices at the reviewed amounts; and
4. a local presentation sidecar and approved WebP assets.

The Stripe test Price creation and the subsequent preflight are the only human-console steps in
this slice. A later M8c deployment tests one paid and one native-free checkout, then records
Stripe/D1 evidence as before.
