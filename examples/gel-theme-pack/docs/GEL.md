# GEL v1 contract

The merchant file defines identity; the theme argument defines presentation. Switching from Minimal to Studio or Classic does not edit or copy the merchant file.

## Token tiers

Global tokens hold literal brand values: `--gel-brand`, `--gel-font-heading`, `--gel-radius`, `--gel-gap` and the other configured roles.

Semantic tokens describe use: `--color-page`, `--color-action`, `--color-on-action`, `--color-highlight`, `--color-rule`, `--color-focus`, etc.

Component tokens bind the common components: `--button-bg`, `--button-fg`, `--card-radius`, `--card-gap`, `--field-border`, `--page-max`.

Themes alter composition and their own layout defaults, while continuing to use the same brand roles, fonts and assets. Components do not contain merchant-specific hex colours. Illustration palettes belong to the supplied artwork; replacing artwork is an asset choice, not a theme fork.

## Configuration

`identity` contains key, full/short names, wordmark descriptor, tagline, description, contact address, locale and currency. No merchant-specific brand text is hardcoded in the theme renderers.

`assets` contains local logo, inverse logo and favicon paths; hero image and alt text are optional together. Optional Stripe PNG logo/icon sources support the hosted Checkout handoff. Local image content is checked before it is copied. SVGs must be passive vector artwork; no scripts, event handlers, foreignObject, remote references or embedded entities.

`colours` accepts exactly the documented hex roles. The compiler rejects unsafe contrast rather than silently changing a merchant's chosen brand. Choose a sufficiently dark brand for button/link use and a light on-brand foreground; configure accent/on-accent independently.

`typography` chooses curated CSS system stacks: editorial, modern, humanist or system. No font files are shipped. The same selected heading/body stacks apply in all three themes.

`shape.corners` is square, soft or round. `layout.density` is compact, comfortable or spacious. They influence common component geometry and spacing; they do not override minimum interactive targets.

`hero` is plain escaped copy. The compiler does not accept HTML fragments, CSS or script values.

## Determinism and provenance

Each build emits `gel.resolved.json` and `build-receipt.json`. The GEL hash is canonical JSON, independent of formatting or field order. Asset bytes are hashed separately. Source/content/theme versions and resolved tokens are explicit. Output contains no build timestamp. Two builds with the same inputs and paths produce the same bytes.

Brand choices never influence order identity, price authority, payment verification, review eligibility, invitation expiry, suppression or delivery semantics.
