CREATE TABLE publication_artifacts (
  digest TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  source_origin TEXT NOT NULL,
  source_scope TEXT NOT NULL,
  canonical_origin TEXT NOT NULL,
  projection TEXT NOT NULL,
  html TEXT NOT NULL,
  jsonld TEXT NOT NULL,
  media TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER publication_artifacts_no_update BEFORE UPDATE ON publication_artifacts BEGIN SELECT RAISE(ABORT, 'immutable publication'); END;
CREATE TRIGGER publication_artifacts_no_delete BEFORE DELETE ON publication_artifacts BEGIN SELECT RAISE(ABORT, 'immutable publication'); END;
CREATE TABLE publication_retired (digest TEXT PRIMARY KEY);
CREATE TABLE publication_tombstones (record_id TEXT PRIMARY KEY);
CREATE TABLE publication_media_tombstones (media_sha256 TEXT PRIMARY KEY);
CREATE TABLE publication_gate (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  digest TEXT NOT NULL REFERENCES publication_artifacts(digest),
  state TEXT NOT NULL CHECK (state IN ('active', 'safe')),
  epoch INTEGER NOT NULL CHECK (epoch > 0)
);
