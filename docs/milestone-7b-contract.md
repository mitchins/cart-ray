# Milestone 7b contract: synthetic presentation compiler

## Scope

Milestone 7b implements the M7a presentation-sidecar contract using only synthetic test products,
descriptions, and generated gradient WebP fixture covers. It parses and joins the local sidecar,
validates static assets, publishes public description/image fields, and renders them in the test
storefront.

It does not import business product data, publish business artwork, add product-detail pages,
deploy a changed Worker or Pages project, enable live mode, retrieve Sheets/CMS/Dropbox content,
or change checkout, D1, Stripe, fulfilment, or Valet authority.

## Compiler boundary

`CsvPresentationSourceAdapter` accepts the three-column local sidecar and validates exact-key
coverage against the complete commerce catalogue, including inactive products. Every `image_key`
must resolve to a local `storefront/assets/products/<image_key>.webp` asset with a WebP file
signature before the storefront build may succeed.

`PresentedCatalogue` adds only public `short_description`, trusted static `image_url`, and an
independent `presentation_version` to the public manifest. It preserves the established commerce
`version` unchanged. The browser continues to send only the commerce version, product keys, and
quantities to checkout.

The Worker composes the same synthetic presentation records only for its public `/catalogue`
response. Checkout continues to construct and validate the commerce catalogue without presentation
data. Neither code path makes a spreadsheet, CMS, or media-provider runtime request.

## Storefront rendering

The storefront renders the public image as a lazy, decorative image and renders the short
description using `textContent`. Presentation CSV values are therefore not interpreted as HTML or
Markdown. The fixture images are deliberately non-business gradients used only to test static asset
publication and layout.

## Acceptance tests

The slice proves that:

1. malformed, ambiguous, duplicate, incomplete, or unmatched presentation records fail closed;
2. static image keys produce only local WebP asset paths and missing/non-WebP assets fail the build;
3. presentation changes affect only `presentation_version`, not the checkout catalogue version;
4. public API/preview output contains descriptions and image URLs without Stripe or fulfilment
   values; and
5. both preview and production storefront builds copy the validated static assets, and preview
   configuration references only their local public paths.

The next catalogue-publishing slice may replace the synthetic local inputs with an explicitly
reviewed small real CSV subset and its local private fulfilment expansions. It must still preflight
against Stripe test Prices before any sandbox deployment.
