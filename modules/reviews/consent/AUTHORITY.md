# Purchaser consent authority (test-only slice)

This module provides an optional same-origin checkout gateway and a private
consent authority. It does not implement the purchase-evidence service, enable
collection, send email, or qualify a live deployment. Both functions require
`ACORN_PURCHASE_CONTEXT=test`, `REVIEWS_STORE_KEY=acorn-store`,
`REVIEWS_ENVIRONMENT=test`, an HTTPS `STOREFRONT_ORIGIN`, a dedicated
`CONTEXT_DB`, and private `CHECKOUT` and `PURCHASE_EVIDENCE` service bindings.
Missing optional context dependencies leave a successful CartRay checkout
response usable without a cookie. The gateway must be served as the actual
first-party `/checkout` route, and only the result from the bound CartRay
service is used to create a context; browser-submitted session IDs never are.

The gateway stores a SHA-256 digest of a 256-bit random cookie, bound to the
returned test Checkout Session, for two hours. The cookie is Secure, HttpOnly,
SameSite=Lax, host-only and scoped to `/`. The first-party consent bridge calls
the authority over a **private** binding; never route the authority publicly.
The bridge supplies origin checks, admission limits and exact input shapes.
Each authority read resolves the stored session through `PURCHASE_EVIDENCE`.
That independently authenticated service receives the session ID and the cookie
digest over its private binding. It must return the exact `session_id`, `context_hash`,
`store`, `environment`, `confirmed`, `source_attested`, `event_id`,
`acquisition_id`, versioned `source_provenance`, `recipient_context_hash` and
`binding_version` and an opaque `recipient_reference`. It must verify the actual store, CartRay D1 resource,
installation, Stripe account, session and immutable catalogue; an
operator-provided label or an arbitrary boolean from an untrusted source is not
attestation. Until such a binding is implemented and hosted-qualified this
module is not safe to enable.

The private `PURCHASE_EMAIL_TRANSPORT` sends a six-digit test verification code
to the source-provided recipient reference; it is not an invitation. The code is
HMAC-protected, scoped to the cookie digest, expires after ten minutes and has
at most five attempts. Delivery is claimed once; a failed, ambiguous or
interrupted delivery is held without automatic resending. A separate secret
`PURCHASE_CODE_KEY` is required. No production transport is supplied.
After a successful code, the bridge signs the stable recipient proof with a
distinct `REVIEWS_RECIPIENT_EVIDENCE_SECRET` and submits it to the existing
Reviews intake endpoint. A retry or context refresh resubmits the same proof;
if intake is unavailable, the browser is not told verification is complete.

Grants use a stable, immutable record keyed by source event, with acquisition,
provenance, recipient binding version and grant time. Changed source or
recipient bindings fail closed. The existing Reviews endpoint remains the
consumer of the bridge's signed evidence and is responsible for applying its
consent-withdrawal tombstones. A grant alone is not permission to send: the
existing Reviews recipient-evidence and withdrawal gates remain required,
including any separate email-possession proof. No address is accepted from the
browser or returned to it.

Run `node --test modules/reviews/consent/tests/authority.test.mjs` and
`uv run --locked python modules/reviews/consent/tests/authority_workerd.py`.
The latter uses real local Workerd/D1, but a synthetic evidence binding. It is
not hosted identity or end-to-end consent qualification.

`purchase_evidence.mjs` implements the private test Stripe/D1 reader behind the
T1 `/attest-purchase` RPC. It validates the signed installation proof and exact
runtime versions, then checks the context database, confirmed CartRay order and
outbox, the Stripe account, paid test Checkout Session, metadata and matching
line items. It queries the attested Reviews service and requires an existing
held intake before any code can be sent, so pre-activation purchases remain
excluded. An explicit `APPROVED_TEST_EMAIL` limits the sandbox to one mailbox.
The public consent authority receives only a scoped hash and opaque reference.
The separate authenticated `/delivery-recipient` endpoint resolves that
reference solely for the private test-code transport. Stripe-reported contact
is purchase evidence, not proof of mailbox ownership; verification still
requires the code.

`test_email_transport.mjs` is an opt-in **test-only** SMTP2GO adapter for the
verification code, never review invitations. It requires one independently configured,
SMTP2GO-verified `TEST_SENDER`, an API key, the one approved test
mailbox, and separate private transport and recipient-resolution tokens. It
records one delivery claim per context and holds failures/unknown outcomes
without resending. SMTP2GO can return HTTP 200 with an application error, so the
adapter requires one success, no failures, and a provider email ID before it
reports acceptance. Provider credentials, sender verification, hosted binding
and real email delivery are not supplied or qualified by these local tests.
The sender is a single plain mailbox; the adapter does not establish domain
ownership or verify it with SMTP2GO. Configure and verify that sender with the
provider before enabling the transport. The approved recipient remains an exact
single-mailbox match, and no caller may select a different destination.

This implementation uses SMTP2GO's documented [standard email API](https://developers.smtp2go.com/reference/send-standard-email).

## Same-origin test storefront routing

An Acorn build with `--review-consent` emits a Pages advanced `_worker.js` and
sets the storefront API base to `window.location.origin`. The normal build does
neither. Configure that Pages deployment with the exact HTTPS
`ACORN_STOREFRONT_ORIGIN`, a `CONSENT_GATEWAY` service binding to the attested
composed authority Worker in `worker.mjs`, and a separate `CHECKOUT` binding to
the attested CartRay source Worker. Pages supplies its `ASSETS` binding. Only
`/checkout`, `/catalogue`, `/checkout-status` and the four explicitly listed
consent endpoints are forwarded; internal evidence and transport routes return
404. If the optional consent gateway is missing or unavailable, checkout routes
fall back to CartRay using the same request/idempotency key; consent requests
fail closed. The gateway does not provide merchant administration.

The composed authority needs its own `CHECKOUT`, `SOURCE_IDENTITY`, `ATTESTOR`,
`REVIEWS_API`, `COMMERCE_DB`, `CONTEXT_DB` and `CF_VERSION_METADATA` bindings.
Hosted installation must verify both the Pages service-binding targets and the
authority's bindings before use. The current local fixture uses synthetic
services; it does not attest Pages deployment configuration or prove hosted
browser routing. `--test-checkout-api` remains an explicit test-build gate and
must identify the intended test deployment; when `--review-consent` is also
selected, the generated browser client calls `window.location.origin` so the
HttpOnly host cookie remains first-party.

Non-deployable examples are provided at `wrangler.authority.example.toml` and
`examples/acorn-store/wrangler.pages.example.toml`; their resource identifiers
are deliberately invalid and all authority modes are disabled. Provision the
actual test resources separately. The authority requires distinct secrets:
`STRIPE_TEST_KEY`, `PURCHASE_ATTESTATION_TOKEN`, `PURCHASE_ATTESTATION_KEY`,
`RECIPIENT_HASH_KEY`, `RECIPIENT_RESOLVE_TOKEN`, `PURCHASE_CODE_KEY`,
`PURCHASE_TRANSPORT_TOKEN`, `REVIEWS_AUTOMATION_INGRESS_TOKEN`,
`REVIEWS_CONSENT_SECRET`, `REVIEWS_CONSENT_RATE_KEY`,
`REVIEWS_RECIPIENT_EVIDENCE_SECRET`, `SMTP2GO_API_KEY`, and the single
`APPROVED_TEST_EMAIL`. Never put these values in the example files, repository,
logs, or public build. In addition, the producer's installation attestation
must use the same test account, Worker versions, D1 IDs, subject mapping and
separate attestation secrets. Version changes require re-attestation.

## Suppression-key safety (test only)

Automatic purchase evidence requires an immutable, installation-scoped suppression
key registry in the purchaser-context D1 database. Apply
`migrations/0002_suppression_key_registry.sql`, set a stable
`RECIPIENT_HASH_KEY_VERSION`, and pin the existing high-entropy key once using
`tools/pin_suppression_key.py`. The tool requires a mode-0600, independently
verified, quiesced snapshot of the correct Reviews D1 and a mode-0600 key file.
If any v2 bindings or suppressions already exist, it refuses to proceed without
a mode-0600 known-contact file derived from an authoritative purchase and a hash
match against that existing history. An empty snapshot is accepted only for a
verified genuinely empty installation. The operator must verify the snapshot's
source, scope and freshness and stop concurrent writers; the tool cannot attest
an arbitrary local file or police a separate deployment. Apply its private SQL
output only to the verified context database; retain it securely and remove it
when no longer needed. A missing row, changed key or changed version fails closed
before purchase evidence or recipient delivery. This is a guard, not a rotation
mechanism; it does not change existing v2 pseudonyms.

**Do not change or retire the suppression HMAC key yet.** Hashes cannot be
translated without the trusted purchase contact. Existing links can unsubscribe
after a cutover, so copying only current suppression rows would lose later
withdrawals. A safe future migration must retain old-key verification, map every
old contact through authoritative purchase evidence, atomically bridge aliases
for both old and new links, fence old writers during cutover, and verify complete
coverage including stale links and concurrent unsubscribe before key retirement.
Until that path is implemented and qualified, key rotation and production release
remain on hold. Never overwrite the registry or delete suppression rows to force
an upgrade.
