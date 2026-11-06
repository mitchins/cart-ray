-- Bound staff history reads to a single store, environment and review.
CREATE INDEX native_revision_review_idx
ON native_revisions(store_key, environment, review_id, revision_id);
CREATE INDEX native_audit_review_idx
ON native_audit(store_key, environment, review_id, event_id);
