# Accessibility implementation and verification

The storefront presentation targets WCAG 2.2 AA. Automated checks are evidence
for individual behaviours, not a full conformance certification.

## Implemented

- Semantic header, named navigation, main/footer, one h1 per route, and skip link.
- Links navigate and buttons act; purchasing/review controls do not require hover.
- Visible keyboard focus, reduced motion, forced-colour styles and mobile reflow.
- Labelled search, sorting, quantities and review fields. Connected star radios
  synchronize to the unchanged native client's hidden rating field; the native
  client still owns validation, drafts, requests and stable retry keys.
- Purchased-product choice uses labelled buttons with `aria-pressed` state.
- Polite status announcements and outcomes that do not rely only on colour.
- Connected withdrawal uses a native modal dialog with cancel, safe initial
  focus, Escape and focus restoration; confirmation delegates to the native
  management client. Native dialog modality contains keyboard focus.
- Generally 44–48 CSS-pixel control targets and 16px mobile form text.
- GEL validation checks text contrast (4.5:1) and control/focus contrast (3:1).
- Decorative adjacent artwork avoids duplicate link announcements; product
  detail artwork has an alternative. No carousel, required drag or timed form.

## Executed

The [connected browser CI](https://github.com/mitchins/cart-ray/actions/runs/42775210696)
passed 300 checks across Chromium, Firefox and WebKit on all three Squirrel themes and Northline Classic.
Desktop/tablet/mobile overflow, reduced motion, labelled review controls, native
withdrawal and Escape focus restoration are included. Review API responses in
this lane are explicit fixtures. The independent HTTP Chromium preview harness
passed 193 composition/interaction checks across all three Squirrel themes and
Northline Classic, with additional 320px reflow and keyboard/modal assertions.
See `../evidence/INTEGRATION.md` for the exact coverage distinction and screenshot
artifact. These were HTTP runs; historical source-pack offline evidence does not
substitute for the integrated results.

No axe audit or screen-reader audit has been performed. VoiceOver/NVDA spoken
announcements, rotor/navigation order, live-region experience and native radio
interaction must still be checked with actual assistive technology. Text resize,
spacing, 400% zoom and forced colours also remain manual checks. Dynamic review
content and hosted Stripe redirect/return need the same practical pass.
