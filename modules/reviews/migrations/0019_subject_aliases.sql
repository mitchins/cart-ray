-- Explicit, immutable aliases belong to a pinned historical publication.
CREATE TABLE review_subject_aliases (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  alias TEXT NOT NULL, subject_id TEXT NOT NULL,
  PRIMARY KEY(store_key, environment, snapshot_id, alias)
);
CREATE INDEX review_snapshot_subject_path_idx
  ON review_snapshot_subjects(store_key, environment, snapshot_id, canonical_path);
CREATE INDEX review_native_subject_path_idx
  ON review_subjects(store_key, environment, canonical_path);
