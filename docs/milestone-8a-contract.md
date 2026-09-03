# Milestone 8a contract: deployable catalogue compiler

## Scope

M8a makes the reviewed synthetic CSV inputs the source of the immutable catalogue bundle consumed
by the Worker and Pages build. It replaces the handwritten synthetic runtime tuples with the
generated private module `src/cartray/compiled_catalogue.py`.

It does not import business product data or artwork, deploy a Worker or Pages project, alter D1,
create or update Stripe objects, enable live mode, retrieve Sheets/CMS/Dropbox data at build or
customer-request time, or change checkout, settlement, fulfilment, or Valet authority.

## Compiler inputs and output

The compiler accepts the existing local reviewed inputs:

```text
catalogue.csv
fulfilment-expansions.json       private
catalogue-presentation.csv
storefront/assets/products/*.webp
price-resolutions.json           synthetic test fixture only
```

It validates the existing commerce, fulfilment, presentation, and WebP-asset contracts, then
renders `compiled_catalogue.py`. The generated module contains normalized commerce rows, private
fulfilment expansions, presentation records, a private `bundle_version`, and the independent
public `presentation_version`. It contains no credential and is never copied into Pages output.

Run or verify it with:

```sh
uv run python scripts/compile_catalogue.py \
  --catalogue fixtures/catalogue.csv \
  --price-resolutions fixtures/price-resolutions.json \
  --fulfilment-expansions fixtures/fulfilment-expansions.json \
  --presentation fixtures/catalogue-presentation.csv \
  --product-assets storefront/assets/products \
  --output src/cartray/compiled_catalogue.py --check
```

`--check` fails if the checked-in generated module differs from those inputs. The Pages build and
the Worker source-sync step run the equivalent check before producing an artifact, so stale
generated source cannot be previewed or deployed accidentally.

## Runtime boundary

The Worker rebuilds its checkout catalogue from the compiled sources and the existing Stripe Price
resolver. It then composes the public presentation manifest from the same compiled sources. Pages
production continues to fetch that public manifest from the Worker; it does not construct a
checkout catalogue of its own.

The synthetic fixture Price IDs deliberately differ from the Stripe test Price IDs. Consequently,
M8a does **not** treat the fixture-derived commerce digest as a production Stripe lock. The browser
continues to submit only the Worker-derived commerce `catalogue.version`, product keys, and
quantities. `bundle_version` is private compiler provenance, and `presentation_version` remains
public rendering/cache provenance only.

A later preflight-linked deployment contract may deliberately lock a reviewed Stripe-resolved
catalogue snapshot. It must not be inferred from fixture Price IDs.

## Acceptance criteria

M8a proves that:

1. the generated Worker module is deterministic for the current synthetic source files;
2. the Worker and Pages preview use the same compiled normalized sources;
3. generated output drift blocks Worker source sync and storefront builds;
4. fulfilment expansions and compiler provenance are absent from the public manifest and Pages
   output; and
5. presentation values remain checked against the compiled `presentation_version` before the
   Worker publishes them.

The next slice can add a controlled real-product test subset only after its source CSV,
presentation sidecar, local assets, private fulfilment expansions, and Stripe test preflight result
are explicitly reviewed.
