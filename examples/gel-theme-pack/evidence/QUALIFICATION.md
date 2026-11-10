# Theme-pack qualification — v1.0.0

## Executed and passed

| Check | Result |
| --- | --- |
| Node preview/state unit tests | 15 passed |
| Python GEL/build/connector contract-fixture tests | 14 passed |
| Chromium presentation/interaction assertions | 193 passed |
| Source JavaScript syntax | Checked with Node |
| Python source compilation | Passed |
| Deterministic build | Byte-identical output across two clean builds |
| Shared GEL identity | Squirrel's Minimal, Studio and Classic builds use the same canonical manifest SHA-256 |
| White-label variant | Northline uses the same Classic CSS with a different GEL |
| Responsive screenshots | 36 PNGs: 4 builds × (3 homepage viewports + 2 product + 2 cart + 2 review) |
| Preview HTML output | 53 pages including the gallery |
| Runtime dependencies | No frontend framework, CDN, remote fonts, analytics or service worker |

`unit-test-output.txt`, `unit-results.json`, `browser-results.json`, `gel-contrast-results.json` and `source-checksums.json` retain the detailed evidence. Each generated storefront also includes its own build receipt and resolved GEL.

## What the browser lane establishes

Chromium rendered the actual generated documents, styles and original artwork. Tests exercised catalogue filters/search/sort, quantity edits, optional review permission, the preview payment handoff, all five purchase-return states, two-product review progress, rating-only and comment-only submissions, review editing, deliberate withdrawal, first-action unsubscribe and suppression on a later simulated purchase.

The same lane checks narrow reflow, image alternative attributes, page heading counts, error focus, keyboard focus, dialog containment/Escape/restoration and reduced-motion behaviour. No application JavaScript exceptions were observed.

The original source-pack authoring evidence used an offline Chromium loader.
The `browser-results.json` in this integrated tree has since been replaced by the
HTTP Chromium harness run against a local server. Its HTTP label is intentional.
That preview run remains separate from connected cross-engine and hosted service
evidence recorded in `INTEGRATION.md`.

## Explicitly not claimed

- No full upstream CartRay or Reviews test suite was run here.
- No live or hosted sandbox purchase, Stripe redirect, email, review API, moderation or publication was exercised here.
- The connected adapter was checked using its explicit ABI fixture, not the entire current repository.
- No axe-core, Safari, Firefox, VoiceOver or NVDA audit was performed.
- This is not a WCAG conformance certificate or production activation approval.

The connected implementation and current repository/hosted qualification are
recorded in `INTEGRATION.md`. Historical source-pack limitations above describe
the original deliverable; they do not replace the current evidence or expand it
into unperformed assistive-technology or production checks.
