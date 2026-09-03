# Milestone 6c sandbox preflight acceptance evidence

## Boundary

This records a single successful CartRay catalogue preflight against Stripe **test mode**. It does
not deploy a Worker or Pages project, write D1, create or change a Stripe object, import a business
catalogue, enable live mode, or reveal fulfilment resources, Price IDs, credentials, or customer
data.

## Observed command

On 2026-09-03, the operator ran the merged preflight command from the CartRay repository with its
test-mode `STRIPE_API_KEY` supplied through the shell environment:

```sh
uv run python scripts/preflight_catalogue.py \
  --catalogue fixtures/catalogue.csv \
  --fulfilment-expansions fixtures/fulfilment-expansions.json
```

The command returned one successful sanitized JSON result with these facts:

| Field | Observed value |
| --- | --- |
| Status | `ok` |
| Stripe mode | `test` |
| Stripe API version | `2025-09-30.clover` |
| Product count | 4 |
| Active product count | 4 |
| Catalogue version | `sha256:14dc0bcd5e3bac001f229446ffe0273dc6a6a9abfa63a832f0c2b6323608e12e` |

The emitted public manifest contained only the four synthetic test products, public titles,
amounts, currencies, and quantity limits. It contained no Stripe Price IDs, private fulfilment
expansions, API credential, authorization header, or raw Stripe response.

## Conclusion

M6c proves that the M6b preflight resolves the synthetic CSV catalogue against the intended Stripe
test-mode objects and produces the expected public manifest version. This is a test-only operator
acceptance check, not authorisation to import real product data or alter the deployed checkout
catalogue. A later controlled-import slice must separately review the source CSV, corresponding
private expansions, and Stripe test lookup keys before any deployment.
