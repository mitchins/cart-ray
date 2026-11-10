# Repository integration qualification

The supplied design-pack evidence is reference evidence from its source branch.
This file records checks rerun against the integrated CartRay tree.

- Gallery built using the supplied compiler: Squirrel Minimal, Studio, Classic,
  and Northline Classic. Supplied HTTP Chromium harness: 193/193 checks pass.
  Generated responsive screenshots stay local and are excluded from Git.
- GEL Python suite: 14 pass. Actual Acorn builder suite: 11 pass, including
  identical GEL hash across themes, unchanged native clients/core, retained
  approved checkout and review gateway, disabled checkout/Reviews configuration.
- Connected adapter Node regressions: 5 pass, covering actual catalogue price
  and quantity authority, false merchant checkbox default, quantity changes,
  pending/unknown locking, cancelled/expired unlocking, matching confirmed cart
  clearing and changed cart retention, storage denial and checkout retry.
- Existing storefront Node suite: 15 pass. Existing Acorn public review,
  consent and gateway Node suites: 7 pass. Existing build/artifact Python: 13 pass.
- `connected-browser-results.json` records completed real-adapter HTTP Chromium
  checks, using explicit test responses for native review API requests. These
  exercise mobile reflow, reduced motion, quantity limits, product selection,
  stars synchronized to native fields, rating-only/comment-only payloads, stable
  retry keys after a 503 response, no bearer capability in storage, native
  management withdrawal and Escape focus restoration, invalid invitation and
  script error checks. This is integration testing, not hosted service evidence.

## Remaining qualification

Cached Firefox fails before creating a page on this macOS host: sandbox extension
permission failure and `RenderCompositorSWGL failed mapping default framebuffer`.
Cached WebKit starts but times out navigating even to the local HTTP root.
No successful Firefox/WebKit customer-flow claim is made. The harness supports
`--engines chromium,webkit,firefox` for a supported browser host.

A VoiceOver/NVDA pass and approved hosted sandbox purchase, authoritative return,
invitation delivery, submit/edit/withdraw, unticked permission and Reviews outage
remain release checks. No production activation or hosted payment/email claim is
made by the theme integration.
