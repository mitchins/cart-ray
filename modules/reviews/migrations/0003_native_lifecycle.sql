CREATE TABLE native_capabilities (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, token_hash TEXT NOT NULL,
  reviewer_key TEXT NOT NULL, subject_id TEXT NOT NULL, expires_at INTEGER NOT NULL,
  used_at INTEGER, PRIMARY KEY(store_key, environment, token_hash)
);
CREATE TABLE native_reviews (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, review_id TEXT NOT NULL,
  subject_id TEXT NOT NULL, reviewer_key TEXT NOT NULL, state TEXT NOT NULL
    CHECK(state IN ('pending','published','rejected','withdrawn')),
  revision_id TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY(store_key, environment, review_id),
  UNIQUE(store_key, environment, subject_id, reviewer_key)
);
CREATE INDEX native_public_idx ON native_reviews(store_key, environment, subject_id, state, created_at, review_id);
CREATE INDEX native_pending_idx ON native_reviews(store_key, environment, state, review_id);
CREATE TABLE native_revisions (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, revision_id TEXT NOT NULL,
  review_id TEXT NOT NULL, author TEXT NOT NULL, title TEXT, body TEXT NOT NULL,
  rating INTEGER CHECK(rating IS NULL OR rating BETWEEN 1 AND 5),
  PRIMARY KEY(store_key, environment, revision_id)
);
CREATE TABLE native_requests (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, request_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL, token_hash TEXT NOT NULL, review_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  PRIMARY KEY(store_key, environment, request_key)
);
CREATE TABLE native_audit (
  event_id TEXT PRIMARY KEY, store_key TEXT NOT NULL, environment TEXT NOT NULL,
  review_id TEXT NOT NULL, revision_id TEXT NOT NULL, action TEXT NOT NULL,
  actor TEXT NOT NULL, occurred_at INTEGER NOT NULL
);
CREATE TABLE native_effect_outbox (
  event_id TEXT PRIMARY KEY, store_key TEXT NOT NULL, environment TEXT NOT NULL,
  subject_id TEXT NOT NULL, kind TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE native_rate_limits (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, action TEXT NOT NULL,
  window INTEGER NOT NULL, count INTEGER NOT NULL CHECK(count <= 30),
  PRIMARY KEY(store_key, environment, action, window)
);
CREATE TABLE native_guard (ok INTEGER NOT NULL CHECK(ok = 1));
CREATE TABLE native_publication_versions (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, subject_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  contribution_count INTEGER NOT NULL DEFAULT 0 CHECK(contribution_count >= 0),
  rated_count INTEGER NOT NULL DEFAULT 0 CHECK(rated_count >= 0 AND rated_count <= contribution_count),
  rating_sum INTEGER NOT NULL DEFAULT 0 CHECK(rating_sum >= rated_count AND rating_sum <= 5 * rated_count),
  PRIMARY KEY(store_key, environment, subject_id)
);
