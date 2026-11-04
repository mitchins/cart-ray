# CartRay Reviews

Reviews is an optional first-party module with a separate Worker and database. Installing CartRay core does not install, bind, schedule or enable Reviews. The checked-in Worker configuration and operator tools are **local test only**: they cannot provision or publish production resources. The Worker defaults to `disabled`; `read_only` exposes only scoped GET endpoints. It provides no collection, invitations, checkout subscription or scheduled task.

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

`GET /reviews/v1/subjects/example-policy/summary` and `/reviews/v1/subjects/example-policy/contributions` are scoped to the Worker's configured store and environment. Lists use a limit of 1–50 (default 20); use the returned `next_after` together with `snapshot_id` as `?after=...&snapshot=...` for the next page. A changed publication invalidates an old cursor. Order is descending by the original source calendar day, with unknown days last and IDs breaking ties; this deliberately does not invent timezone or time-of-day precision. Replies accompany their selected parent. Only published, allowlisted content is returned. Reads require an imported approved snapshot in this slice; a fresh empty install needs no import and returns not-found until a subject is published.

This bounded local slice accepts at most 200 subjects, 200 contributions and 90,000 bytes of ASCII-escaped snapshot JSON in one import. The importer rejects larger inputs instead of publishing a partial snapshot. These limits are implementation bounds, not claims about the historical source inventory.

Run `PYTHONPATH=modules/reviews/src uv run pytest modules/reviews/tests` and `uv run python modules/reviews/tests/workerd_integration.py`. The latter creates a disposable local D1 database and exercises the explicit installer, ingestion, read-only API and disabled Worker with no D1 binding. SQLite `SnapshotStore` remains an earlier independent rehearsal, not the D1 implementation.

## Boundaries still open

The snapshot contract and installer are not qualified for production or historical data. Hashing proves byte identity, not authorship, approval, privacy, completeness or freshness. Public prose needs human privacy review and escaped rendering. Source-specific evidence, raw records and media must remain restricted. Original local/GMT timestamp provenance, source completeness, reconciliation, stale publication and withdrawal policy require source-backed qualification before a real import or Astro coupling. Legacy WordPress migration is an optional adapter, not a prerequisite for a fresh store. This slice does not deploy a Worker, publish production content or change legacy writers.

PR #33's earlier public commits and automated review comment exposed non-secret deployment fingerprinting details; the merged tree redacts them, but that does not remove public history or comments. GitGuardian passed on that PR and a targeted scan for common private-key and token patterns found no matches in its commits. This is not proof that every possible credential format is absent; no credential rotation was performed based on those checks.
