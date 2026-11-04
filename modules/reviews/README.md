# CartRay Reviews snapshot experiments

This opt-in Python package currently contains a provisional public-snapshot validator and a local SQLite publication-store rehearsal. It does not change CartRay core or deploy a Worker, provision resources, export WordPress, import source records, render pages, collect reviews or send mail. It is not a complete R1/R2 implementation and its schema is not yet approved for real-data import or renderer coupling.

`load_snapshot` validates an independently pinned SHA-256, store and environment. The embedded source completeness assertion is not evidence by itself: a source manifest and independent reconciliation are still required. Hashing proves byte identity, not source authenticity, approval, privacy or freshness. A reviewer must approve the actual public content, including personal information in free text; consumers must escape body and author text.

The provisional schema models review and reply identities, subject-scoped rating summaries, distinct verification assertions, anchor aliases, vote totals and the source's original date string, declared precision and timezone provenance. Unknown vote totals remain null. Its treatment of original local and GMT timestamps, source field completeness, capture/publication freshness, trusted-source verification and material change reconciliation requires further design and source-backed tests before use. Media, source archival and the complete projection are not yet implemented.

The local `SnapshotStore` stores exact immutable artifact bytes and advances a publication pointer in one SQLite transaction. Same bytes and identity replay as a no-op; changed bytes with the same identity or a stale/conflicting publication revision fail. A failed transaction leaves the old pointer intact. This proves only the local rehearsal behaviour, not D1 transaction behaviour, reconciliation or a production publication process.

Run `PYTHONPATH=modules/reviews/src uv run pytest modules/reviews/tests`. Real-source qualification, D1/Workerd, packaging and deployed UAT remain open. No automatic production refresh, collection, sending or legacy writer cutover is enabled.
