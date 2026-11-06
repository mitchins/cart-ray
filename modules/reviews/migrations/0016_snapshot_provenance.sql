CREATE TABLE review_snapshot_provenance (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  source_identity TEXT NOT NULL, extracted_at TEXT NOT NULL,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
  source_complete INTEGER NOT NULL CHECK(source_complete = 1),
  PRIMARY KEY(store_key, environment, snapshot_id)
);
