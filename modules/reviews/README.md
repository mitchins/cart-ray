# CartRay Reviews

Reviews is an optional first-party module. The merchant installation uses one physical D1
with separate commerce and Reviews tables, a private native Reviews Worker, the Commerce
Worker and one private transactional email handler. Checkout does not depend on Reviews
service availability; a shared D1 outage affects both. Live collection and sending remain
disabled. See [the operator guide](operator/README.md) and [order Reviews](../../docs/order-reviews.md).

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

Confirmed-order processing owns the receipt and optional follow-up. Merchant preferences
select off, receipt, follow-up or both, and all eligible products or a deterministic
priority product. The default is receipt inclusion and priority selection. All purchased
eligible subjects remain accessible; selection controls presentation, not entitlement.

Capabilities are created from confirmed paid purchases and the recorded checkout permission,
independently of email acceptance. New links do not expire by default. Existing issued links
retain their original validity, generation, revocation and submitted-subject progress.
Recovery uses native invitation generations through the same merchant email provider and
bounded commerce cron. Unknown provider outcomes are held, never blindly resent.

Suppression removes optional review solicitation, including across purchases, without
suppressing the transactional receipt. No separate Reviews sender, recipient OTP, intake
queue, due queue or sender activation is provisioned. Historical migrations, audit evidence
and retained legacy records are not deleted or automatically replayed. Retained old hosted
installations remain operated by their pinned release until a separately authorized handover.

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
The accepted state filters are `uncertain`, `dispatching`, `failed`, `accepted` and `revoked`; each page is scoped to its store and environment and uses an `after` delivery-ID cursor. The response includes the acquisition reference for authorized operations staff, but excludes recipient addresses and bearer capabilities. Staff can inspect stored provider observations for a delivery. Provider acceptance and mailbox delivery are distinct. There is no staff action to resend or resolve an uncertain delivery; authoritative operational reconciliation remains a release requirement.

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

### Historical automation compatibility

Native internal automation modules and SQL history retain validation/read/recovery semantics
for existing records and issued capabilities. They are not a deployable default email pipeline.
The retired JavaScript producer, consumer, due scheduler, initial sender, recovery sender and
post-checkout verification Workers are removed. Their old qualification receipts remain history;
new ordinary order processing is exercised by `tests/test_order_reviews.py` and the connected
Workerd/D1 smoke test.
