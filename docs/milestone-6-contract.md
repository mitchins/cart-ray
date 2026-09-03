# Milestone 6a contract: catalogue source protocol

## Scope

Milestone 6a makes CSV the first business-authoring interchange without coupling CartRay to Excel,
Google Sheets, or another spreadsheet runtime. It introduces an adapter boundary before any real
catalogue is imported.

It does not fetch Google Sheets, parse XLS/XLSX, add a database catalogue, deploy a changed Worker,
or introduce real product, Dropbox, Valet, or customer data. CSV is the only concrete input in this
slice.

## Protocol

The catalogue builder accepts one asynchronous `CatalogueSourceAdapter`:

```python
class CatalogueSourceAdapter(Protocol):
    async def load(self) -> tuple[CatalogSource, ...]: ...
```

An adapter supplies normalized `CatalogSource` records only. The established catalogue builder
continues to own price resolution, fulfilment-expansion validation, manifest versioning, and public
versus private manifest separation. It also revalidates every adapter result before Price
resolution, so a future adapter cannot bypass the CSV contract.

The protocol is asynchronous so a later adapter can retrieve a *pinned export* from a service such
as Google Sheets. It must still return the same normalized records before the catalogue build begins.
No customer request may make a spreadsheet or external catalogue call.

## CSV interchange

`CsvCatalogueSourceAdapter` loads a UTF-8 file containing exactly these headers:

```text
product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active
```

The source is deliberately narrow:

- `product_key` is the permanent, source-neutral identifier.
- `stripe_lookup_key` identifies the server-resolved Stripe Price; the CSV never contains a Price ID
  or amount supplied by a browser.
- `fulfilment_type` and `fulfilment_version` are opaque references to private expansion data; the
  CSV never contains Dropbox links, private object names, or credentials.
- `max_quantity` is a product policy, not stock accounting. Digital inventory remains out of scope.
- `active` controls sale availability in the generated public catalogue.

The parser rejects a missing, duplicate, renamed, or extra header; empty, short, or over-wide rows;
unknown boolean values; invalid product keys; non-positive quantities; and duplicate product keys.
It reads only a deterministic local UTF-8 file during build or test work.

## Future adapter rule

A Google Sheets adapter, if later required, is only an ingestion layer. It must export or retrieve
an explicitly selected revision, validate it into this exact record contract, and hand it to the
same builder. It cannot change the CSV schema, bypass validation, become a Worker runtime
dependency, or carry price, fulfilment, or customer authority.

## Acceptance tests

The slice proves that:

1. the existing synthetic CSV, a bundled static adapter, Worker catalogue construction, and preview
   storefront construction all use the same normalized policy;
2. CSV and a non-CSV adapter producing equal records yield byte-equivalent catalogue semantics and
   manifest version;
3. malformed CSV is rejected before conversion or price resolution;
4. public manifests remain free of Stripe Price IDs and fulfilment resources.

The next slice may use this protocol to validate a sanitized real CSV against Stripe test Prices.
