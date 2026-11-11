# Product gallery presentation contract

The existing three-column presentation CSV remains valid. Authors may add either or both optional
columns `image_alt` and `images_json`. `image_key` always selects the primary image. `image_alt`
is its plain-text alternative description; when absent, the public product title is used.

`images_json` contains an ordered JSON array of additional image objects, for example:

```json
[{"image_key":"example-detail.png","alt":"Detail of the template layout"}]
```

Use normal CSV quoting when storing JSON in a CSV cell. An omitted or empty cell means no extras.
Every extra requires exactly `image_key` and nonempty `alt` strings. At most seven extras are allowed
(eight images including primary). Duplicate image references, unknown object properties, surrounding
whitespace, external URLs, paths, unsupported extensions, and non-string values fail validation.

Extensionless keys retain the existing `/assets/products/<key>.webp` mapping. Explicit `.webp`,
`.png`, `.jpg`, `.jpeg`, and `.svg` filenames are accepted with the same lowercase alphanumeric and
hyphen stem grammar. Build validation checks every referenced asset, its file signature, and that
resolved paths remain in the static product asset directory. SVG assets are local authored static
assets; the signature check does not sanitize arbitrary SVG content.

Public products retain `image_url` for the primary image and add `images`, an ordered array of
`{"url": "/assets/products/example-detail.png", "alt": "Detail description"}` objects with primary
first. Alt text is plain text and must be rendered using text/attribute APIs. No browser-supplied
presentation field gains checkout authority. Gallery order and alt changes affect only the independent
`presentation_version`; the commerce catalogue version and checkout contract remain unchanged.

Compiled bundles preserve the old serialization for products without optional metadata. Gallery
metadata travels through the existing immutable build-time presentation source and never requires a
runtime media provider or authoring service.

The deployment compiler fingerprints the bytes of every referenced primary and extra image when
`product_assets` is supplied. These private build-time SHA-256 values are carried in the compiled
presentation source, so changing bytes at the same filename changes `presentation_version` without
changing the commerce version. They are never exposed as checkout input. Historical bundles without
fingerprints continue to load; new deployable builds should always supply their product asset folder.

Connected themes retain the catalogue build's product images when a theme supplies a file at the
same path. Merchant-authored product descriptions should disclose illustrative mock-ups when needed;
the optional GEL `hero.image_caption` provides a visible disclosure for hero artwork. Image alt text
describes meaningful images, while decorative product-card images retain empty alternatives.
