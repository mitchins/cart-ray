# CartRay — three themes, one merchant identity

A working visual implementation for Codex: **Minimal**, **Studio** and **Classic**, compiled from one small, versioned merchant GEL file. This source-only GitHub deliverable includes the implementation, integration adapter, original artwork, overview previews and executable tests. Prebuilt `dist/` outputs and screenshots can be regenerated locally.

## Start with the preview

From the repository root (Python 3.10+, no build-time dependencies):

```sh
cd examples/gel-theme-pack
python3 build.py --all --output dist
python3 serve.py --directory dist --port 4173
```

Open `http://127.0.0.1:4173/`. Do not double-click the HTML: browser modules need an HTTP origin.

The gallery links to:

| Build | Presentation |
| --- | --- |
| `/minimal/` | Squirrel: quiet editorial spacing, open product cards and a restrained header |
| `/studio/` | The **same Squirrel GEL**: dark brand canvas, larger type, asymmetric cards and expressive imagery |
| `/classic/` | The **same Squirrel GEL**: prominent search, category navigation, retail grid and conventional purchase controls |
| `/northline-classic/` | The **same Classic theme** with a different fictional merchant, wordmark, colours, typography, shape and density |

Every build has catalogue browsing, category filtering, search, sorting, product details, persistent cart, quantity editing, optional review-invitation choice, payment handoff, five purchase-return states, an illustrative email, multi-product review submission, review editing/withdrawal and unsubscribe. The free digital item does not receive a review invitation.

**The preview never charges, sends email or calls a commerce API.** Payment and review states are explicitly simulated. Preview storage uses `cartray.theme-demo.*`, not CartRay's live cart or capability storage. Reset it through “Preview journeys & states” at the bottom of a page.

## Rebuild

```sh
python3 build.py --all --output dist-new
python3 serve.py --directory dist-new

# One merchant + one theme, with no runtime theme selector:
python3 build.py --theme studio \
  --gel gel/squirrel.json --catalogue content/squirrel.json \
  --output squirrel-studio
```

The builder refuses to overwrite an existing directory. Build output includes resolved global/semantic/component tokens, manifest and asset hashes, a file receipt, and a Stripe branding handoff. No timestamps or host paths enter the preview build receipt.

## GEL / white-labelling

Change `gel/squirrel.json`, **not the theme code**. Theme selection is a separate build argument, not part of the brand.

The manifest contains identity, wordmark descriptor, logo variants, favicon, optional hero artwork and alternative text, semantic colour roles, curated system-font stacks, corner treatment, density and hero copy. `gel/brand-manifest.schema.json` supports editor validation. `src/gel.py` additionally validates local asset content, safe paths and contrast. It rejects unknown keys, CSS injection, unsupported font choices, missing assets and active SVG content.

All theme text colours are drawn from the same brand roles. Studio uses the inverse logo and on-brand foreground rather than inventing another brand palette. Typography choices are intentionally limited; no font files, web-font calls or arbitrary merchant CSS are included.

`content/*.json` is **presentation/sample catalogue content, not a payment authority**. Squirrel uses the existing `ACORN-KIT`, `ACORN-PLANTER`, `ACORN-GUIDE` keys and canonical product paths. Prices are sample AUD 24 / 18 / 0 values in preview mode only. Northline is a wholly fictional second merchant.

## Connect to CartRay without rewriting checkout

Read `CODEX_HANDOFF.md` before integration. `connect.py` operates on a **previously built Acorn storefront**, writes a new output directory, and makes no deployment or activation changes.

```sh
python3 connect.py \
  --cartray-root /path/to/cart-ray \
  --source-site /path/to/existing-acorn-build \
  --gel gel/squirrel.json \
  --catalogue content/squirrel.json \
  --theme minimal \
  --output /path/to/new-themed-acorn-build
```

The connected adapter imports the existing CartRay client exports for catalogue loading, cart persistence, checkout creation and purchase-return reconciliation. It does **not** implement its own settlement protocol. It uses the server catalogue for current prices and product limits; initial static price labels are neutral until that catalogue is loaded. Review access, submission, management capability handling and revision/idempotency rules remain in the original standalone clients, copied byte-for-byte.

Only the copied cart module's final auto-boot statement is removed, so the new presenter can call its unchanged exports. The original repository and source build are not modified. An unexpected export/boot pattern, product route or review subject fails the integration build with a diagnostic. `integration-receipt.json` records source hashes and the exact edit.

Connected builds omit the demo adapter, sample inbox, simulated payment and unsubscribe pages. Existing same-origin gateways and flags are retained; **no test/live flag is enabled**. The existing server unsubscribe endpoint remains authoritative. GEL does not grant purchase, review or email permission.

## Accessibility

Implemented with a WCAG 2.2 AA design target: semantic landmarks, one page heading, keyboard navigation, a first-focus skip link, visible focus, real labels and fieldsets, status announcements, error focus, explicit withdrawal confirmation, focus return, reduced motion, forced-colour styles and responsive reflow. Text/foreground pairs are validated at 4.5:1 and essential control/focus pairs at 3:1; controls generally have 44–48px targets. Mobile form fields use 16px text.

This is **not a blanket WCAG certification**. The package contains targeted automated checks and visual evidence. Safari/Firefox and real VoiceOver/NVDA testing remain integration checks. See `docs/ACCESSIBILITY.md`.

## Tests

```sh
npm test                         # Node 22+ and Python; no npm install needed
python3 tests/browser_test.py     # Playwright + Chromium, local preview server running
```

Set `CHROMIUM_EXECUTABLE` when Chromium is installed elsewhere. `requirements-dev.txt` lists the optional browser test dependency.

The retained original-package browser evidence used `--offline` because URL navigation is disabled by the supplied Chromium policy in the authoring environment. That lane renders the actual generated HTML, CSS and SVGs and executes the same application source, with module loading and navigation replaced by the documented harness. It tests presentation and interactions, not an HTTP deployment. The ordinary HTTP lane is also included for Codex to run.

## Contents

- `src/`: GEL compiler, templates, shared CSS, three layouts and shared presenter/adapters.
- `gel/`: two merchant manifests and JSON Schema.
- `content/`: synthetic catalogue/presentation fixtures.
- `assets/`: original SVG artwork and ready-to-upload PNG branding sources.
- `dist/`: generated on demand by `build.py`, intentionally not committed.
- `screenshots/`: generated by the documented browser harness; not committed.
- `THEMES-OVERVIEW.webp`, `WHITELABEL-OVERVIEW.webp`: compact visual references for GitHub review.
- `tests/`, `evidence/`: executable tests and honest qualification results.
- `CODEX_HANDOFF.md`: integration boundaries and the remaining repository-specific acceptance.

This is a presentation deliverable, independent of OPERABILITY-01, hosted key rotation, Effective Policy/Woo migration and receipt-only review delivery.
