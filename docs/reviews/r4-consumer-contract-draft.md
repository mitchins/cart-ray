# Reviews consumer contract: R4 draft

This describes the experimental mixed-source test response and unresolved
publication requirements. It is not approval to couple Astro or publish data.

The Reviews Worker is independently deployed and scoped to one configured
store and environment. Consumers use `GET /reviews/v1/subjects/{subject}/summary`
and `GET /reviews/v1/subjects/{subject}/contributions?limit=...`. A subject key
must resolve through a separately reviewed registry of canonical product, service
or store identities and canonical site paths; the Reviews Worker does not infer a
subject from a WordPress numeric ID. Keep explicit historical aliases and account
for retired and ambiguous products. The registry, host origin and the mechanism
for cross-account fetch or deployment-time transfer must be explicitly configured
and access controlled; a shared Worker or D1 binding is not assumed.

For an overlapping test subject, the summary contains `subject_id`, an opaque
`snapshot_id`, `rating.average`, `rating.rated_count` and `contribution_count`.
Unrated contributions count only in the latter. The contributions response has
`subject_id`, `snapshot_id`, `contributions`, `next_after` and `has_more`.
Source-prefixed IDs distinguish historical (`h-`) and native (`n-`) records;
`source_id` retains the underlying identity. Imported entries retain their source
date object, title, legacy verification claim, approved fragment anchors, vote
totals and nested replies. Native entries expose their creation time and may
claim `cartray_verified_acquisition` only from native evidence. Public fields are
allowlisted; source email addresses, identities used for redemption, internal
capabilities and moderation metadata are private.

Ordering compares the source calendar day for imported records and UTC calendar
day for native records, newest first, followed by source-prefixed ID. It does not
claim historical timestamps without authoritative timezone or precision. Request
a later page using both `after` and the returned `snapshot` value. The composite
version covers the imported publication and native publication version; a change
invalidates the cursor. A renderer must restart on a stale or changed publication
and must escape all text. The test Worker uses `Cache-Control: no-store`.

A future Astro build must pin and independently approve one complete publication
revision, generate HTML and JSON-LD from the same approved data, and preserve the
legacy fragments supplied by the projection. Product-only structured data must
represent reviews of that actual product; general service or store experiences
cannot be reclassified as product reviews. Do not add schema aggregates to empty
or unrelated products. The release process must specify how approvals, corrections
and withdrawals invalidate API/CDN caches and remove content from generated HTML,
JSON-LD and associated media, including rebuild failure and rollback. No stale
period has been approved; automatic production refresh is not authorized.

Before this contract is qualified, implement and validate evidence-backed
cross-source identity reconciliation, persist and expose appropriately scoped source identity and extraction provenance,
resolve source mappings and historical fields, prove publication atomicity and rollback in D1, and specify the static
withdrawal mechanism. In particular, the test projection currently counts
source records independently and cannot distinguish a historical review from a
native representation of the same review. Live overlapping reads are disabled.
