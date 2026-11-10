# Repository integration qualification

Qualified source: `e151f7946e6ac15c731fef5c05c6ed38a485c97d`.
The supplied design-pack evidence is reference evidence from its source branch;
this file records checks rerun against integrated CartRay.

## Automated browser evidence

[CI browser job](https://github.com/mitchins/cart-ray/actions/runs/42771171192/job/129679062232)
passed **72/72 connected checks**: 24 each in Chromium, Firefox and WebKit, using
Playwright 1.57 on Ubuntu 24.04. The checked-in
`connected-browser-results.json` is copied from that job's artifact.

[Download screenshots, result JSON and HTTP log](https://github.com/mitchins/cart-ray/actions/runs/42771171192/artifacts/13742237547).
The artifact contains 15 actual connected screenshots: desktop/tablet/mobile
home plus mobile cart/review in each engine. GitHub currently retains it until
8 February 2027; generated screenshots are excluded from the source deliverable.

This completed connected browser lane uses **Classic with Squirrel**.
The expanded CI now builds all three Squirrel compositions and an explicit
Northline Classic synthetic catalogue contract fixture, then runs the same three
engines against each. Theme-labelled result JSON and screenshots keep their
coverage separate. At this recording the expanded run is pending; its setup is
not counted as an executed qualification result. The Northline fixture uses the
real core/adapter/native clients, has checkout disabled, and is never installed
as merchant catalogue data.

The completed lane It exercises the
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

- GEL Python: 14 pass. Actual Acorn builder: 11 pass, including one identical GEL
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

At this recording, hosted withdrawal and a complete newly delivered invitation
journey remain unclaimed. No production payment or email claim follows from the
fixture-based browser results.
