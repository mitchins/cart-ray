# Milestone 7a contract: catalogue presentation sidecar

## Scope

Milestone 7a defines how CartRay will add public product descriptions and images without changing
the established commerce CSV or adding a spreadsheet, CMS, database, or media-provider dependency
to the Worker or storefront runtime. It is a contract only: it does not import a business catalogue,
publish an image, generate a deployment bundle, add product pages, or change the deployed test
storefront.

The existing seven-column `catalogue.csv` remains the authority for saleable commerce facts:

```text
product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active
```

It deliberately remains free of presentation URLs, HTML, Markdown, Stripe Price IDs supplied by a
browser, and fulfilment resources.

## Presentation interchange

A local UTF-8 `catalogue-presentation.csv` will use exactly these headers:

```text
product_key,short_description,image_key
```

- `product_key` is the same permanent source-neutral key in the commerce CSV.
- `short_description` is required public plain text. It is rendered as text, never inserted as
  HTML or interpreted as Markdown. Rich content, long-form pages, translations, and SEO routing
  are later concerns.
- `image_key` is required and identifies a versioned static image asset. It is not a URL, path,
  filename, Dropbox reference, or external media identifier.

Every cell must be non-empty and free of leading/trailing whitespace. `product_key` must satisfy
the existing product-key grammar. `image_key` uses lower-case letters, digits, and hyphens only,
begins with an alphanumeric character, and is limited to 81 characters. Duplicate product keys,
unknown product keys, and a missing presentation row for any commerce product fail the build.

The exact-key join includes inactive products as well as active ones. `active=false` hides the
product from the public catalogue, but does not permit its presentation record to drift or vanish.

## Static image publication

M7b will map an `image_key` to a static Pages asset:

```text
image_key: sil-template-cover-v1
→ /assets/products/sil-template-cover-v1.webp
```

The image file is authored and published with the storefront build; it is not fetched from Sheets,
Dropbox, or another media service at customer-request time. A changed image uses a new key, such as
`sil-template-cover-v2`, rather than mutating the meaning of an existing key. This makes the
published reference cache-safe and reviewable.

M7b will verify that every referenced asset exists, has the permitted `.webp` form, and is included
in the generated static storefront output. Alternative storage such as R2 may later implement the
same immutable `image_key → public URL` boundary, but does not change this input contract.

## Public composition and versions

The deployment compiler will join validated commerce and presentation records before producing the
public storefront catalogue. A visible product will eventually have this public shape:

```json
{
  "product_key": "EXAMPLE-POLICY",
  "title": "Example policy",
  "short_description": "A concise public description.",
  "image_url": "/assets/products/example-policy-cover-v1.webp",
  "amount_minor": 2500,
  "currency": "aud",
  "max_quantity": 1
}
```

`catalogue.version` remains the existing commerce version used by checkout validation. Presentation
changes must not invalidate an otherwise valid cart or become checkout input. The compiler will add
a separate public `presentation_version` for storefront caching and rendering only; the browser
never submits it to `/checkout`.

The public output contains descriptions and public image URLs only. Stripe lookup keys, resolved
Price IDs, fulfilment versions/resources, credentials, and raw source data remain private.

## Future source adapters

CSV is the first concrete authoring interchange. A future Google Sheets or CMS adapter may retrieve
an explicitly selected export and normalize it to the same presentation record contract during the
build. It cannot fetch on a customer request, alter the commerce CSV schema, bypass the join or
asset checks, or give an external source authority over checkout facts.

## Acceptance criteria for M7b

1. the compiler rejects malformed headers, whitespace ambiguity, duplicate or unmatched keys, and
   missing presentation records;
2. it verifies every active and inactive commerce record has one presentation record and every
   image key has an allowed static asset;
3. public output includes escaped plain descriptions and deterministic static image URLs, but no
   Stripe or fulfilment private values;
4. changing presentation changes only `presentation_version`, not `catalogue.version` or checkout
   behaviour; and
5. the Worker, Pages storefront, and test fixtures continue to have no runtime spreadsheet, CMS,
   Dropbox, or other media-provider call.
