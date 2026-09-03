# Milestone 6b contract: Stripe test catalogue preflight

## Scope

Milestone 6b turns the CSV interchange into an operator preflight. It validates one explicit local
UTF-8 CSV and one explicit local fulfilment-expansions JSON file against Stripe **test-mode** Prices
before a catalogue could be deployed. It does not alter the Worker runtime, deploy a Worker or
Pages project, write D1, create or update any Stripe object, fetch Google Sheets, parse XLS/XLSX,
or introduce customer or live-mode data.

The preflight command is deliberately a build/operator operation, not a customer-request path:

```text
spreadsheet editor
  → exported UTF-8 CSV + private local fulfilment expansions
  → CartRay preflight (read-only Stripe test validation)
  → deterministic public catalogue result
```

Stripe state is an input to this command, so the operation is not described as offline. The source
files remain local and deterministic; no external catalogue source is fetched.

## Command and credentials

Run the command with explicit file paths:

```sh
uv run python scripts/preflight_catalogue.py \
  --catalogue path/to/catalogue.csv \
  --fulfilment-expansions path/to/fulfilment-expansions.json
```

`STRIPE_API_KEY` is read only from the environment. There is no command-line key argument and the
command does not load `.env` files. It accepts only `rk_test_…` or `sk_test_…` keys; a missing,
publishable, or live key fails before any network request. Use a restricted, read-only sandbox key
where Stripe permissions permit it.

The transport permits only bounded HTTPS `GET` requests to `https://api.stripe.com`, carries the
existing pinned Stripe version, caps response bodies, rejects redirects before following them, and
has no create, update, delete, or checkout operation. Its strict Product inspection is preflight-
specific; the deployed Worker retains its established Price resolver and permissions.

## Inputs and validation

The CSV remains the exact seven-column M6a protocol. A UTF-8 BOM and leading or trailing whitespace
in a value are rejected rather than silently changing a lookup key or catalogue digest.

The fulfilment-expansions JSON is an explicit private input. It maps non-empty version strings to
non-empty, unique resource identifier lists. It is used only to build the established immutable
catalogue; its values are never emitted by the command.

Every CSV row, including `active=false` rows, resolves through the normal builder. `active=false`
means hidden from the public catalogue, not exempt from Price or fulfilment validation. An inactive
or retired Stripe Price/Product therefore fails this preflight; supporting retired historic records
would be a separate semantics change.

For each lookup key, CartRay requires exactly one Stripe Price that is:

- active, test-mode, one-time, per-unit, fixed-amount, and non-negative;
- explicitly returned with the exact requested lookup key; and
- expanded to an active, test-mode Stripe Product.

The preflight fails closed for missing, duplicate, incomplete, recurring, custom-amount, live-mode,
inactive, or incorrectly keyed objects.

## Output

On success, stdout is one stable, sorted JSON line with:

- `status: "ok"`, `stripe_mode: "test"`, and the pinned API version;
- the existing public catalogue manifest and its version; and
- total and active product counts.

It never prints the API key, authorization header, Stripe response, private manifest, Price IDs, or
fulfilment resources. Failure writes the fixed `catalogue preflight failed` diagnostic to stderr and
returns non-zero without a traceback.

## Acceptance tests

The slice proves that:

1. equal inputs produce the same catalogue version, public manifest, and serialized output;
2. the command uses read-only, pinned-version Stripe requests and does not expose private values;
3. invalid credentials and malformed expansions make zero Stripe requests;
4. inactive CSV rows still resolve but are absent from the public manifest; and
5. missing, ambiguous, inactive, live-mode, recurring, custom-amount, unexpanded, or mismatched
   Stripe objects are rejected.

The next slice may introduce a controlled small real-product CSV import only after its source data,
private fulfilment expansions, and Stripe test Price lookup keys are explicitly reviewed.
