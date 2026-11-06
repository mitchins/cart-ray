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
version covers the imported publication and native publication version. Explicit
identity links atomically increment the native version; a change invalidates the cursor. A renderer must restart on a stale or changed publication
and must escape all text. The test Worker uses `Cache-Control: no-store`.

A future Astro build must pin and independently approve one complete publication
revision, generate HTML and JSON-LD from the same approved data, and preserve the
legacy fragments supplied by the projection. Product-only structured data must
represent reviews of that actual product; general service or store experiences
cannot be reclassified as product reviews. Do not add schema aggregates to empty
or unrelated products. The release process must pin a manifest identifying the source and publication
revision used for both HTML and JSON-LD. On a withdrawal, invalidate the API
projection, switch affected routes to a previously prepared safe fallback that omits
the review content and structured data, and purge affected cached pages and media.
Produce a reviewed replacement build that removes the content from HTML, JSON-LD
and copied media. Keep the fallback active until the replacement is confirmed; a previous build containing withdrawn content
is not an acceptable rollback target. If rebuilding or purging fails, retain the
safe fallback and alert the operator for manual recovery. The actual fallback
switch, cache inventory and cross-account operator access require an operational
implementation and rehearsal. No maximum stale period has been approved; automatic
production refresh is not authorized.

A test-only pinned identity manifest can link a reviewed historical root to an
approved native revision without heuristics. The immutable link suppresses the
old root, preserves its anchors, source-date and verification attribution plus
historical replies, and prevents withdrawal from resurrecting it. The linked
native review controls visibility. Snapshot promotion fails closed if it would
remove or change linked historical content or its descendants while the native
review can still be published. After native withdrawal, a newer snapshot may
omit that history; the immutable link remains as a tombstone. The current
implementation also relies on retained historical snapshots, provenance and
contributions to recognize withdrawn descendants across later imports. Do not
garbage-collect that ancestry until descendant tombstones have been persisted
independently and qualified. Reviewed source corrections and the operational
withdrawal workflow still require qualification.

Before this contract is qualified, validate reconciliation completeness and
approval, expose appropriately scoped source attribution, resolve source
mappings and historical fields, prove publication atomicity and rollback in D1, and specify the static
withdrawal mechanism. Unlinked source records count independently: the system cannot infer whether
they are the same review. Live overlapping reads remain disabled until an
explicit per-subject reconciliation and approval gate is implemented.
