# CartRay Reviews

Reviews is an optional first-party module with a separate Worker and database. Installing CartRay core does not install, bind, schedule or enable Reviews. The checked-in Worker configuration and operator tools are **local test only**: they cannot provision or publish production resources. The Worker defaults to `disabled`; `read_only` exposes only scoped GET endpoints. Collection modes work **only in the test environment** and fail closed in live. There are no checkout subscriptions or scheduled invitation tasks.

The intended primary collection mode is `verified_acquisition_only`. Its current implementation is test-only: a separately authenticated synthetic settlement adapter reads confirmed paid CartRay orders and version-pinned product mappings, then issues an opaque invitation for eligible paid subjects. The invitation is bound to an opaque recipient context supplied by that trusted test issuer; CartRay's current settlement record does not establish a recipient identity. A production adapter must obtain and bind that context from a trusted source. Possession can be transferred by forwarding the link; it does not establish the bearer's personal or email identity. A purchase can produce at most one review per eligible subject. Free lines are not labelled verified purchases. GET and HEAD do not redeem invitations. Revoking an invitation stops further use and does not withdraw an existing review. Key rotation currently fails closed for existing issuance and does not reissue tokens. No invitation is sent automatically, and staff authentication and production qualification remain release requirements.

The local collect simulation requires a configured test administrator token and a short-lived, one-use test capability issued by that administrator. It does not verify a real email address, offer self-service anti-abuse verification, or qualify collection for staging or production. Native guest submissions start pending; authenticated test moderation approves, rejects or withdraws them. Only approved native revisions enter public reads. Real guest verification, a qualified anti-abuse provider, a staff identity integration, and the static-site withdrawal policy remain activation gates. WordPress tooling is not a runtime dependency.

Direct Workerd and a disposable hosted test have exercised incomplete uploads; the hosted edge reset both test requests after approximately 15 seconds while other requests remained available. The local Wrangler proxy has independently crashed on the same D1-free reproducer. Its original diagnostic remains available with `REVIEWS_PROXY_STALL_DIAGNOSTIC=1` and is not part of the normal local lifecycle gate; direct Workerd is tested separately. Hosted evidence is synthetic and does not authorize staging or production activation.

Native moderation updates the published rating totals, publication version, audit record and invalidation outbox atomically. Summary reads use those totals; they do not scan all reviews. The local Worker returns native responses with `Cache-Control: no-store` so withdrawal is not delegated to a browser or CDN cache.

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

`GET /reviews/v1/subjects/example-policy/summary` and `/reviews/v1/subjects/example-policy/contributions` are scoped to the Worker's configured store and environment. Lists use a limit of 1–50 (default 20); use the returned `next_after` together with `snapshot_id` as `?after=...&snapshot=...` for the next page. A changed publication invalidates an old cursor. Imported reviews order by original source calendar day with unknown days last; native reviews order by submission time. IDs break ties. Replies accompany their selected parent. Only published, allowlisted content is returned. A fresh native subject produces an empty summary without importing anything. Native and imported subjects cannot currently share an identity; an explicit unified projection is required before such a migration.

This bounded local slice accepts at most 200 subjects, 200 contributions and 90,000 bytes of ASCII-escaped snapshot JSON in one import. The importer rejects larger inputs instead of publishing a partial snapshot. These limits are implementation bounds, not claims about the historical source inventory.

Run `PYTHONPATH=modules/reviews/src uv run pytest modules/reviews/tests`, `uv run python modules/reviews/tests/workerd_integration.py`, and `uv run python modules/reviews/tests/native_workerd.py`. The Workerd tests use disposable local D1 databases. SQLite `SnapshotStore` remains an earlier independent rehearsal, not the D1 implementation.

## Boundaries still open

The snapshot contract and installer are not qualified for production or historical data. Hashing proves byte identity, not authorship, approval, privacy, completeness or freshness. Public prose needs human privacy review and escaped rendering. Source-specific evidence, raw records and media must remain restricted. Original local/GMT timestamp provenance, source completeness, reconciliation, stale publication and withdrawal policy require source-backed qualification before a real import or Astro coupling. Legacy WordPress migration is an optional adapter, not a prerequisite for a fresh store. This slice does not deploy a Worker, publish production content or change legacy writers.

PR #33's earlier public commits and automated review comment exposed non-secret deployment fingerprinting details; the merged tree redacts them, but that does not remove public history or comments. GitGuardian passed on that PR and a targeted scan for common private-key and token patterns found no matches in its commits. This is not proof that every possible credential format is absent; no credential rotation was performed based on those checks.
