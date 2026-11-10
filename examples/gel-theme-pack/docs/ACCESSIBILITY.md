# Accessibility implementation and verification

Design target: WCAG 2.2 AA for the storefront presentation. This is a target, not a certification of the connected commerce system.

## Implemented

- Semantic header, named navigation, main and footer; one h1 per generated route.
- Skip link appears before preview or site navigation and targets the main region.
- Links navigate, buttons perform actions. No hover-only purchasing or review controls.
- Visible keyboard focus, reduced-motion handling and Windows forced-colour styles.
- Mobile reflow down to 320 CSS pixels; no fixed-width form columns on mobile.
- Labelled search, sorting, quantities and review inputs; rating uses a native radio group in the preview. The connected native review client retains its labelled select until a presentation-only integration is completed.
- Polite status announcements for cart mutations and form outcomes. Validation brings focus to the relevant field. Review submission status is not conveyed by colour alone.
- Withdrawal uses a native modal dialog with a visible cancel action, safe initial focus, explicit Tab wrapping, Escape and focus restoration. The original connected management client's existing confirmation sequence remains unchanged.
- Controls generally have 44–48 CSS-pixel targets. Radio stars have a full labelled hit area. Mobile form text is 16px to avoid small input text.
- Global contrast validator rejects deficient text (4.5:1) and control/focus (3:1) combinations. Studio's inverted surfaces use the on-brand/on-accent roles.
- Product artwork beside a product title is decorative to avoid duplicate link announcements; the product detail illustration has meaningful alt text.
- No auto-advancing carousel, background motion, required drag operation or time-limited form completion.

## Executed here

See `evidence/browser-results.json` for the precise assertion list. Screenshots use the real generated CSS and SVGs in Chromium at 1440, 768 and 390 pixels; narrow reflow is additionally checked at 320 pixels. Browser execution used an offline loader because the provided browser policy blocks URL navigation. The loader is checked in and changes only resource/module loading, storage backing and navigation delivery, not the application decision logic.

The targeted lane checks landmarks/heading structure, image alternatives, keyboard focus, modal behaviour, mobile overflow, forms, error focus, reduced motion, and complete preview purchase/review/opt-out flows. No axe-core engine was available. Do not describe this as an axe audit or a screen-reader audit.

## Remaining connected-site checks

Run the ordinary HTTP browser lane, current repository regression suites, and a short keyboard plus VoiceOver/NVDA pass. Check text resize/spacing, 400% zoom/reflow on the real content, forced colours, browser error states and the hosted Stripe redirect/return. Confirm that dynamic server-supplied review content retains meaningful labels, safe rendering and focus placement.

## Primary references

- WCAG 2.2 quick reference: https://www.w3.org/WAI/WCAG22/quickref/
- WAI modal dialog pattern: https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/
- Stripe hosted Checkout appearance: https://docs.stripe.com/payments/checkout/customization/appearance
