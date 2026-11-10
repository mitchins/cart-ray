# Codex handoff — GEL + Minimal / Studio / Classic

## What has been implemented

This is source code and a runnable visual reference, not a request to design the themes again. Preserve the three compositions and the small GEL model. Build the gallery (`python3 build.py --all --output dist`), inspect both compact overview images and run the responsive browser harness before editing. The GitHub source deliverable excludes generated `dist/` and screenshots.

**Minimal:** spacious split hero, quiet typography, open catalogue cards, calm product and customer forms.

**Studio:** full-width brand canvas, larger display type, angled artwork, staggered product cards, strong section boundaries. It consumes the same Squirrel colours, fonts and assets, including the inverse logo.

**Classic:** logo/search/cart masthead, category bar, category rail, regular product cards, familiar product/cart layouts. Northline proves that a different GEL works without changing this theme.

All share the same interaction code and component vocabulary. Do not create separate checkout, consent, return-state or review implementations for each theme.

## Integration target

The inspected repository contracts were:

- `examples/acorn-store/build.py`, `index.html`, `store.json`: generated static storefront, three mapped products, external `storefront-config.js`.
- `storefront/app.js`: exported cart/catalogue/checkout/return functions plus a final module auto-boot guard.
- `modules/reviews/ui/reviews.js` and `manage.js`: standalone capability-based customer forms.
- `examples/acorn-store/reviews.mjs`: public product review rendering.

This bundle does not change any PR #74 deployment-verification code, Cloudflare configuration, schema migration, secret, email dispatcher or eligibility decision.

### Use the supplied integration path

1. Run the normal Acorn builder from the current repository with its existing approved configuration. Do not create a different checkout backend.
2. Run `connect.py` against that output and the current repository. It checks the expected export/route ABI, writes a new themed site and retains existing configuration and gateways.
3. Integrate the GEL compiler into the existing build entrypoint. Prefer a small `--gel` / `--theme` extension rather than an additional permanent build pipeline. The standalone compiler is deliberately self-contained so this can be done without a new frontend framework.
4. The copied client currently removes only `if (typeof document !== "undefined") boot();`. For the permanent repository integration, expose a clean presenter/library entrypoint or an explicit boot guard. Leave all existing checkout helper function bodies and tests intact.
5. Keep the native customer review and management clients. Their DOM receives the shared theme CSS. The richer preview demonstrates the intended two-product selection and star controls; integrating those presenters with the native clients is permitted, but preserve the native request/retry/capability logic. **The connected adapter currently uses the native select-based rating UI, not the demo's radio-star presenter.** Do not silently substitute preview review persistence for the native service.
6. Let the existing product Reviews reader populate public review data. Do not publish the illustrative quote, rating, fixture email or preview success states in a connected build.

## Non-negotiable boundaries

The browser never establishes payment or review eligibility from an email address, query parameter or session ID alone. Return states delegate to CartRay's existing status/reconciliation functions. Pending and unknown return states retain checkout locking; successful confirmation clears only a matching pending cart. Existing idempotency, trusted recipient, opt-out, suppression, refund/eligibility, activation and replay policies remain server-owned.

Do not persist invitation/management bearer capabilities in theme storage. The original review clients keep their capabilities in fragments and POST bodies, not query strings or analytics. Keep `no-referrer`, no-store review responses and gateway behaviour. No tracking pixels, remote font resources or analytics are added by this package.

Do not make checkout depend on Reviews availability. Conversely, the connected review pages load the native standalone client without first fetching the commerce catalogue.

The checkout checkbox remains merchant-configurable. Squirrel's preview default is checked; the connected presenter reads the actual existing configuration. The theme must not manufacture a grant, classify preselection as express opt-in, or add customer verification screens.

Native unsubscribe HTTP handling is outside this theme's authority. The sample confirmation screen is a visual reference, not a new live endpoint. Preserve the existing GET/HEAD non-mutating behaviour and authenticated/capability-bound POST semantics. Do not add a confirmation screen to the mail-client one-click POST.

## Build and asset policy

The GEL has one schema version and no theme-specific fields. Colour roles, font stacks, logo variants and assets remain stable across themes. Theme files own layout composition; merchant identity is not duplicated per theme.

Generated assets are local. The SVGs are original illustrations, not product photography. PNG logo/icon exports are included for Stripe branding. `stripe-branding.json` is an operator handoff, not an API payload to blindly send: apply only settings supported by the pinned Stripe integration. Never put a review capability into a standard receipt by assumption.

No page builder, CMS, arbitrary merchant CSS, runtime theme engine, new React/Vue dependency, font service or required CartRay attribution is introduced.

## Acceptance to run in the real repository

Use the existing tests rather than launching another security architecture project.

1. All three Squirrel builds use the identical GEL hash. Classic also builds with Northline's materially different GEL. New brand identity does not require code edits.
2. Run the supplied Node/Python tests and the HTTP Chromium browser lane. Check Safari/WebKit and Firefox for the customer flow and native `<dialog>`/focus behaviour. Perform a short VoiceOver or NVDA pass; the included automated checks are not a substitute.
3. Run CartRay's existing storefront and Reviews tests unchanged. Add presenter regression coverage around quantity controls and the actual pending/unknown/expired/confirmed/cancelled return paths, including storage denial and retry behaviour.
4. Use an existing approved hosted sandbox to verify catalogue → product → cart → Stripe redirect → authoritative return → native invitation → submit → edit/withdraw. Exercise the unticked checkbox and Reviews outage. No production activation is implied.
5. Check the live customer review UI with a two-product invitation, rating-only and comment-only submissions, an invalid/expired invitation and a lost acknowledgement. Keep native idempotency keys stable on retries.
6. Check the actual public review projection and no-index/no-store policy. Do not introduce review structured data or fake counts.

The integration helper has contract-fixture tests, not an end-to-end qualification against the full current CartRay tree. That is the remaining integration work. No hosted claims are made in this bundle.

## Completion scope

Deliver the three themes and basic GEL as one coherent storefront change. Normal CI/review applies. Do not reopen Effective Policy migration, email consent policy, hosted key rotation or control-plane attestation. Do not expand this into a theme marketplace.

The visual design, source assets, preview interactions and deterministic build are supplied. The job is to connect and qualify them against the current shared CartRay application, not invent another storefront.

## Integrated repository implementation

The normal Acorn builder now accepts `--gel`, `--theme`, `--theme-content` and
`--gel-assets`. Connected output copies the core client without text rewriting;
`data-cartray-presenter="gel"` suppresses its default DOM auto-boot. The native
review clients also remain byte-identical. A DOM-only presenter synchronizes
accessible star radios to their native rating field, supplies a purchased-product
chooser and uses the shared native dialog for withdrawal confirmation. Preview
fixtures and reference evidence above are historical source-deliverable context;
see `evidence/INTEGRATION.md` for current repository qualification.
