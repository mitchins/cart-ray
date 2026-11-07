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
line items. An explicit `APPROVED_TEST_EMAIL` limits the sandbox to one mailbox.
The public consent authority receives only a scoped hash and opaque reference.
The separate authenticated `/delivery-recipient` endpoint resolves that
reference solely for the private test-code transport. Stripe-reported contact
is purchase evidence, not proof of mailbox ownership; verification still
requires the code.

`test_email_transport.mjs` is an opt-in **test-only** SMTP2GO adapter for the
verification code, never review invitations. It requires a verified
`TEST_SENDER` under the Acorn test domain, an API key, the one approved test
mailbox, and separate private transport and recipient-resolution tokens. It
records one delivery claim per context and holds failures/unknown outcomes
without resending. SMTP2GO can return HTTP 200 with an application error, so the
adapter requires one success, no failures, and a provider email ID before it
reports acceptance. Provider credentials, sender verification, hosted binding
and real email delivery are not supplied or qualified by these local tests.

This implementation uses SMTP2GO's documented [standard email API](https://developers.smtp2go.com/reference/send-standard-email).
