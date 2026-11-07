# CartRay Reviews

Reviews is an optional first-party module with a separate Worker and database. Installing CartRay core does not install, bind, schedule or enable Reviews. The checked-in Worker configuration and operator tools are **local test only**: they cannot provision or publish production resources. The Worker defaults to `disabled`; `read_only` exposes only scoped GET endpoints. Collection modes work **only in the test environment** and fail closed in live. There are no checkout subscriptions or scheduled invitation tasks.

Staff authentication can explicitly exercise Workerd WebCrypto RS256 verification with
`REVIEWS_STAFF_CRYPTO=webcrypto`; verification errors deny access and never fall back
to the Python test verifier. Optional `REVIEWS_STAFF_ADMISSION=edge` requires a
separate `REVIEWS_STAFF_INGRESS` rate-limit binding, a secret
`REVIEWS_STAFF_RATE_KEY` of at least 32 characters and a valid Cloudflare-supplied
`CF-Connecting-IP`. Missing or unavailable admission fails closed before JWKS
loading. IPv4 addresses use individual buckets; IPv4-mapped addresses share the
corresponding IPv4 bucket, and IPv6 addresses share a /64 bucket. Cloudflare's
rate limits are approximate and per-location, not a global
hard bound. A caller-supplied IP is trustworthy only behind a correctly configured
Cloudflare ingress; the test mode is not itself an authentication proxy.

Optional `REVIEWS_STAFF_JWKS_CACHE_SECONDS` accepts 1–60 seconds (0 disables
caching). Keys are cached per issuer in a bounded per-isolate cache, never served
stale after expiry or on provider failure. The lifetime starts when a fetch begins,
and a fetch that consumes the entire lifetime fails closed. Key removal and new key recognition may
take up to the configured TTL; unknown key IDs do not force unlimited refreshes.
This is a test-qualified adapter, not permission to activate live staff access.
Production policy, edge bindings, rotation/revocation limits, actual provider
behavior and platform deployment still require separate qualification.

The optional test-only automation intake is separately disabled unless `REVIEWS_AUTOMATION_INTAKE=test` and a distinct issuer token is configured. A separately authenticated test activation endpoint captures a primary-session CartRay outbox watermark and records an immutable policy and fence in Reviews D1. The committed source snapshot is the activation boundary: all earlier outbox IDs are fenced without backfill, and later IDs can be processed, including confirmations in the same second. The authenticated endpoint accepts a CartRay outbox ID, rechecks the bound CartRay database for the confirmed paid order and immutable product mapping, and atomically records an independent Reviews acknowledgement and a due-time intent held for eligibility, recipient and consent verification. Earlier events and excessively late events are recorded without an intent. Duplicate delivery is idempotent; conflicting identities fail closed. It never acknowledges or modifies the CartRay outbox, dispatches a queue, creates a capability or sends mail. This local intake is not a production event subscription or a qualified recipient/consent adapter.

For this synthetic adapter, store and environment are operator assertions about the configured CartRay database and issuer token; the source database does not attest that scope. The outbox snapshot is a single primary read, and checkout records and their outbox events commit together. The snapshot and Reviews D1 write are still cross-database operations: a crash before the Reviews commit does not enable intake, and retries capture a new boundary. Source-binding identity remains an operator assertion rather than a source attestation. Trusted source-scope attestation, an activation-boundary handshake, refund/dispute eligibility, recipient and consent verification, and independent subscriber delivery/reconciliation are unresolved requirements before production integration. The test intake acknowledges receipt into Reviews; it does not implement a CartRay outbox dispatcher or advance a subscriber cursor.

For held synthetic intents, a separate test-only recipient evidence adapter verifies a scoped HMAC from its own distinct secret and records an immutable acquisition-bound recipient-context hash, binding version, evidence reference and verification time. It accepts only an already-held intent and never stores an address. The existing independently signed `review_invitation` consent adapter remains the source of purpose-bound consent. An authenticated internal status check reports whether a binding was recorded and whether a non-revoked consent record matches that binding; it exposes neither recipient hash nor evidence reference. The intent remains `held_eligibility_recipient_consent` even when both records exist. A recorded synthetic binding is historical evidence, not proof of current recipient eligibility: recipient correction, refund/dispute checks and production authority are not implemented. A separate signed test-only consent-withdrawal endpoint stores an immutable purpose-and-recipient tombstone and suppresses matching invitation issuance and replacement delivery, including when withdrawal arrives before a grant. Existing purchase-bound review capabilities are not withdrawn merely because communication consent is withdrawn. It does not re-grant consent or recall an email already accepted by a provider. No sender, scheduler or delivery authorization is added.

The intended primary collection mode is `verified_acquisition_only`. Its current implementation is test-only: a separately authenticated synthetic settlement adapter reads confirmed paid CartRay orders and version-pinned product mappings. Signed, purpose-bound consent is recorded separately. Invitation generations start inactive and become usable only after a separately authenticated delivery acknowledgement; expiry defaults to 60 days from the recorded acceptance time. Replacement invalidates the previous capability without resetting the purchase's submitted subjects. Possession can be transferred by forwarding the link; it does not establish the bearer's personal or email identity. A purchase can produce at most one review per eligible subject. Free lines are not labelled verified purchases. GET and HEAD do not redeem invitations. Revoking an invitation stops further use and does not withdraw an existing review.

CartRay's settlement record does not establish a recipient identity. In the qualified test setup, generic recovery requests are dispatched automatically through a private queue to an authenticated delivery worker. The worker checks the trusted synthetic recipient binding and purpose-bound consent; uncertain sends are retained for reconciliation rather than blindly resent. The local Maildir processor and explicitly configured `manual_test` recovery mode remain available for testing, not production delivery. Customer management uses a separate scoped bearer capability for edits and withdrawals. Live sending, independent staff authentication, real recipient integration, provider-receipt lookup, operational reconciliation, and production qualification remain release requirements.

The separate local `collect` simulation requires a configured test administrator token and a short-lived, one-use test capability issued by that administrator. It does not qualify collection for staging or production. Native submissions start pending; authenticated test moderation approves, rejects or withdraws them. Only approved native revisions enter public reads. Independent staff identity and the static-site withdrawal policy remain activation gates. General anonymous guest verification and Turnstile are optional future adapters; the purchase-bound flow does not require them. WordPress tooling is not a runtime dependency.

Direct Workerd and a disposable hosted test have exercised incomplete uploads; the hosted edge reset both test requests after approximately 15 seconds while other requests remained available. The local Wrangler proxy has independently crashed on the same D1-free reproducer. Its original diagnostic remains available with `REVIEWS_PROXY_STALL_DIAGNOSTIC=1` and is not part of the normal local lifecycle gate; direct Workerd is tested separately. Hosted evidence is synthetic and does not authorize staging or production activation.

Native moderation updates the published rating totals, publication version, audit record and invalidation outbox atomically. Summary reads use those totals; they do not scan all reviews. The local Worker returns native responses with `Cache-Control: no-store` so withdrawal is not delegated to a browser or CDN cache.

## Optional WordPress migration rehearsal

The optional read-only exporter and reconciliation tool live under
`migration/wordpress/`; the Reviews Worker does not import them. Run the exporter
against an explicitly identified site and keep its output private. The
reconciliation command takes that restricted export and an explicit subject map,
then writes a new restricted directory containing an incomplete draft and a
per-record report. It accepts only the test environment and does not approve,
import or publish source content. Password-protected or unknown source subjects,
unrecognized approved metadata and approved media requiring disposition fail
closed. A published post without a password is not proof of public visibility:
access-control and catalogue settings still require review. The optional
`--redact-source-emails` flag conservatively replaces literal author-email occurrences in candidate
text with `[email removed]` and records each affected source ID and field in the
restricted report. The original text remains in the restricted export. Remaining
source-email matches fail closed, and snapshot validation rejects email-like public
text until it has an explicit disposition. This is not a substitute for prose review.

For an isolated local test only, `qualify_test_snapshot.py` can create a separate,
byte-pinned snapshot after the operator supplies an explicit graph-scope attestation
and independent review evidence. It recomputes reconciliation, keeps the original
candidate incomplete, and binds the artifact to a test environment and the
WordPress review-comment graph. The evidence hash authenticates bytes, not the
reviewer or their authority. It does not approve production publication or assert
completeness of attachments, external providers or the entire migration.

```
PYTHONPATH=modules/reviews/src uv run python modules/reviews/migration/wordpress/reconcile.py \
  --source /private/review-export.json --mapping /private/subject-map.json \
  --expected-source https://example.invalid --store example-store \
  --output-directory /private/new-reconciliation
```

## Local install and run

Use the repository's pinned Node and Python tooling. From the repository root:

```sh
npm ci --ignore-scripts
uv run python modules/reviews/tools/install.py
PYTHONPATH=modules/reviews/src uv run python modules/reviews/tools/import_snapshot.py \
  modules/reviews/tests/fixtures/approved-synthetic.json \
  --sha256 570f1cb950c2e1b0fd70c0eca8d6550fe2b660fe6746a58e7c4d78dba86f5fa8 \
  --store example-store --environment test
uv run pywrangler dev --config modules/reviews/wrangler.test.toml --local --port 8798 --var REVIEWS_MODE:read_only
```

The installer verifies a checksum ledger, applies missing migrations and rejects unknown, changed or out-of-order migrations. Running it again is a no-op. Start without the mode override to disable all routes. Import is an explicit operator action; the caller must independently approve and pin the exact bytes. Repeating an import is idempotent, while reused identities with changed bytes, revision conflicts and stale revisions are rejected. A failed D1 batch retains the previous publication. The local tools never target a remote D1.

In test collection modes, authenticated staff with `reviews:moderate` can use
`GET /reviews-admin/v1/published` to select a published native review for a
revision-bound withdrawal. Each item includes an approved `subject_name` when
one exists; otherwise it is null. The per-review `/revisions` and `/audit`
reads expose only that scoped native review's history. These lists use opaque
`after` cursors and at most 50 rows and 128,000 serialized bytes per response.
History is ordered by identifier, not chronology; revision timestamps are not
invented. Cursor reads are not snapshot-consistent: concurrent moderation or
new revisions can change later pages, so refresh to obtain a current view. None of these staff endpoints is a production authorization boundary.

The test-only `reviews:moderate` permission can create or edit one staff reply
per published native review through `POST /reviews-admin/v1/contributions/{id}/reply`.
Supply an idempotency key, the current `parent_revision_id`, an
`expected_reply_revision_id` (null for creation), and the intended public
`author` and `body`. Withdrawal uses the same parent/reply revision guards and
idempotency key on `/reply-withdraw`; withdrawn replies cannot be recreated in
this slice. `/reply-revisions` and `/reply-audit` expose staff-only history.
The independently authenticated actor is retained in audit, not in public
content. Each accepted change increments the subject publication version and
invalidation outbox without modifying review or rating totals. A reply is only
public while its parent remains published at the revision it was attached to.
Historical imported reviews and reply reactivation are not supported here.

Staff with the separate `reviews:operations` permission can use test-only
`GET /reviews-admin/v1/deliveries?state=uncertain` to inspect a bounded queue.
The accepted state filters are `uncertain`, `dispatching` and `failed`; each
page is scoped to its store and environment and uses an `after` delivery-ID
cursor. The response excludes recipient, order and bearer-token information.
It reports only recorded states, not reasons or provider acceptance. There is
no staff action to resolve or resend a delivery: authoritative provider receipt
verification and safe operational reconciliation remain unimplemented.

`GET /reviews/v1/subjects/example-policy/summary` and `/reviews/v1/subjects/example-policy/contributions` are scoped to the Worker's configured store and environment. Lists use a limit of 1–50 (default 20); use the returned `next_after` together with `snapshot_id` as `?after=...&snapshot=...` for the next page. A changed publication invalidates an old cursor. Imported reviews order by original source calendar day with unknown days last; native reviews order by submission time. IDs break ties. Replies accompany their selected parent. Only published, allowlisted content is returned. A fresh native subject produces an empty summary without importing anything. When a historical and native subject share the same canonical kind and path, the experimental test-only unified feed orders both by calendar day and source-qualified ID, and binds pagination to both publication versions. Cross-source identity reconciliation is not yet qualified; overlapping subjects fail closed in live.

This bounded local slice accepts at most 200 subjects, 200 contributions and 90,000 bytes of ASCII-escaped snapshot JSON in one import. The importer rejects larger inputs instead of publishing a partial snapshot. These limits are implementation bounds, not claims about the historical source inventory.

Run `PYTHONPATH=modules/reviews/src uv run pytest modules/reviews/tests`, `uv run python modules/reviews/tests/workerd_integration.py`, and `uv run python modules/reviews/tests/native_workerd.py`. The Workerd tests use disposable local D1 databases. SQLite `SnapshotStore` remains an earlier independent rehearsal, not the D1 implementation.

## Boundaries still open

The snapshot contract and installer are not qualified for production or historical data. Hashing proves byte identity, not authorship, approval, privacy, completeness or freshness. Public prose needs human privacy review and escaped rendering. Source-specific evidence, raw records and media must remain restricted. Snapshot v2 retains original local/GMT timestamp strings and public titles. Snapshot v3 adds an explicit public source attribution without exposing the private source-instance identity. Snapshot v4 carries explicitly mapped legacy subject IDs and paths; `/reviews/v1/subject-lookup?alias=...` resolves them without a WordPress runtime. The validator requires declared seconds and microseconds to match their original precision; other fractional precision requires a future explicit schema rather than padding digits. Source completeness, identity reconciliation, canonical mapping, stale publication and withdrawal policy require source-backed qualification before a real import or Astro coupling. Legacy WordPress migration is an optional adapter, not a prerequisite for a fresh store. This slice does not deploy a Worker, publish production content or change legacy writers.

PR #33's earlier public commits and automated review comment exposed non-secret deployment fingerprinting details; the merged tree redacts them, but that does not remove public history or comments. GitGuardian passed on that PR and a targeted scan for common private-key and token patterns found no matches in its commits. This is not proof that every possible credential format is absent; no credential rotation was performed based on those checks.

### Staff identity work in progress

`cartray_reviews.staff_identity` contains an independent Cloudflare Access RS256
assertion and explicit permission verifier. It accepts a trusted, pinned issuer,
audience and subject-to-permission policy; it derives the audit actor identifier
from the verified issuer and subject. The test fixture key is synthetic.

In the explicit test environment, `REVIEWS_STAFF_MODE=cloudflare_access` selects
this boundary for the pending queue and moderation routes, including the audit
actor. It also requires distinct subject-administration permissions. Configured
Access mode never falls back to a test-administrator credential. A synthetic
test-only JWKS fixture is supported for local Workerd qualification. Trusted
policy provisioning, live provider qualification, operations endpoints and a
staff interface remain required. Live collection and moderation stay disabled.
The current Workerd test exercises `collect` mode only. The existing
`verified_acquisition_only` test guard still requires a configured test-admin
secret even when Access authentication is selected; Access-only moderation in
that mode has not been qualified. The Access boundary does not accept that
secret as a fallback identity.

### Synthetic due-intent evaluation (development only)

`automation_due` is a test-only keyed claim primitive, not a scheduler
or delivery authorization. An authenticated test-only HTTP trigger evaluates a
single explicitly addressed intent and durably records its held completion. A trusted future queue consumer must supply an exact
acquisition identity, an authenticated scope, an authoritative clock and a fresh
unpredictable 256-bit claim token. A due held intent can be claimed once; a retry
with the same token observes the lease, and expiry permits a fenced takeover.
Completion records only that this evaluation finished. It does not change the held
intent, create an invitation, check current consent or allow any provider send.

A real integration still requires a qualified event/due-time queue, authenticated
consumer, authoritative source scope and activation watermark, current eligibility
and consent decisions, and an explicit re-evaluation policy. In particular, lease
fencing cannot prevent a paused process from making an external call; provider
idempotency and uncertain-outcome reconciliation remain mandatory before sending.
There is no scheduled task, polling loop, or production path in this increment.
