# Repository integration qualification

Earlier hosted source: `e151f7946e6ac15c731fef5c05c6ed38a485c97d`.
The supplied design-pack evidence is reference evidence from its source branch;
this file records checks rerun against integrated CartRay.

## Automated browser evidence

The latest recorded [CI run](https://github.com/mitchins/cart-ray/actions/runs/42775210696)
passed **300/300** connected checks across all four compositions and three engines.
Its [artifact](https://github.com/mitchins/cart-ray/actions/runs/42775210696/artifacts/13744445081)
contains 60 screenshots, result JSON and logs. The checked-in 72-check JSON below is
historical, not the result of this later run.

[CI browser job](https://github.com/mitchins/cart-ray/actions/runs/42771171192/job/129679062232)
passed **72/72 connected checks**: 24 each in Chromium, Firefox and WebKit, using
Playwright 1.57 on Ubuntu 24.04. The checked-in
`connected-browser-results.json` is copied from that job's artifact.

[Download screenshots, result JSON and HTTP log](https://github.com/mitchins/cart-ray/actions/runs/42771171192/artifacts/13742237547).
The artifact contains 15 actual connected screenshots: desktop/tablet/mobile
home plus mobile cart/review in each engine. GitHub currently retains it until
8 February 2027; generated screenshots are excluded from the source deliverable.

The expanded [CI job](https://github.com/mitchins/cart-ray/actions/runs/42772776530/job/129684213589)
completed **288/288** checks: all three Squirrel compositions and the Northline
Classic contract fixture, each in Chromium, Firefox and WebKit.
[Download 60 screenshots, four result JSON files and HTTP logs](https://github.com/mitchins/cart-ray/actions/runs/42772776530/artifacts/13743345137).
The results were independently downloaded and counted as four sets of 72 passing
checks. Northline uses an explicit synthetic, checkout-disabled catalogue with
matching routes and the real core/adapter/native clients; it is never installed
as merchant catalogue data. `connected-browser-results.json` remains the one-off
72-check reference from the earlier Classic/Squirrel run, not current harness
output. Current results carry a composition and live in the expanded artifact.

The connected lane exercises the
real adapter and unchanged native review clients against explicit review API
fixtures. Checks cover responsive reflow, reduced motion, authoritative quantity
limits and currency, merchant checkout/permission flags, product selection,
stars synchronized to native fields, rating-only/comment-only requests, stable
retry keys after a 503, absence of bearer capabilities in storage, native
withdrawal, Escape focus restoration, invalid invitations and script errors.
It does not establish hosted payment, email delivery or server eligibility.

The supplied HTTP Chromium preview harness additionally passed **193/193**
checks across Squirrel Minimal, Studio and Classic, and Northline Classic.
Those are preview composition/interaction checks. They are separate from the
connected cross-engine qualification above. Initial macOS Firefox launch and
WebKit navigation failures were environment failures; the successful Linux lane
provides the completed cross-engine evidence.

## Regression evidence

- GEL Python: 15 pass. Actual Acorn builder: 11 pass, including one identical GEL
  hash across the three Squirrel themes, unchanged core/native clients, retained
  approved checkout/gateway and disabled checkout/Reviews configuration.
- Connected adapter Node: 5 pass, covering runtime price/quantity/currency,
  configured false permission, quantity changes, pending/unknown locks,
  cancelled/expired unlocks, matching confirmed cart clear, changed cart retention,
  storage denial and checkout retry.
- Existing storefront Node: 15 pass. Existing Acorn review/consent/gateway Node:
  7 pass. Existing build/artifact Python: 13 pass. Repository-wide Ruff passes.
- SonarQube Cloud and SonarCloud Code Analysis pass on the qualified source;
  the PR unresolved-issues API returned zero findings when inspected.

## Approved hosted test evidence

The existing `squirrel-one-d1-test` installation at
<https://squirrel-one-d1-test-store.pages.dev> was upgraded to the themed source;
its retained resources, orders, provider identity and recipient suppression were
preserved. No new production activation was performed.

The hosted operator/browser checks reported an actual two-product checkout with
review permission declined; settlement retained two paid lines and zero selected
review subjects. An existing delivered invitation retained the previously
submitted kit and available planter. A four-star planter review was submitted,
exact-revision approved and read publicly. A subsequent customer edit was
approved and the corrected public projection was read. During a controlled
Reviews 503, another declined checkout settled successfully; the recorded
Reviews version was restored afterward.

The committed `hosted-test-summary.json` is the sanitized operator report.
Private operational receipts under `/tmp/ux01-readiness` contain provider and
resource identifiers and are deliberately not committed. These hosted results
are distinct from API fixture responses in browser CI.

## Accessibility and remaining checks

The implementation targets WCAG 2.2 AA; no full conformance certification is
claimed. Automated keyboard/focus, labels, reflow and reduced-motion checks ran.
An actual VoiceOver/NVDA session has **not** been completed or claimed. Text
resize/spacing, 400% zoom, forced colours and spoken announcements/rotor order
still need an assistive-technology pass on real content.

The PR records a subsequent new sandbox purchase, delivered invitation and review
journey on `b7127f8`. Hosted withdrawal remains unclaimed. No production payment
or email claim follows from fixture-based browser results.

## Follow-up changes after hosted qualification

The first hosted test deployment used `e151f79`. Subsequent review fixes retain
page-specific titles, correct static preview currency and the Minimal shop
selector, selectively retain only declared merchant image assets, and strengthen
browser synchronization/evidence. A later hosted journey used `b7127f8`, as
recorded in the PR. Actual screen reader verification remains unperformed.
