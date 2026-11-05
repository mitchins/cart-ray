CREATE TABLE acquisition_invitations (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('test', 'live')),
  acquisition_id TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  recipient_context_hash TEXT NOT NULL CHECK(length(recipient_context_hash) = 64),
  subject_set_hash TEXT NOT NULL CHECK(length(subject_set_hash) = 64),
  key_version TEXT NOT NULL CHECK(key_version = 'v1'),
  token_hash TEXT NOT NULL CHECK(length(token_hash) = 64),
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  PRIMARY KEY(store_key, environment, acquisition_id),
  UNIQUE(store_key, environment, token_hash)
);
CREATE TABLE native_review_acquisitions (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  review_id TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  PRIMARY KEY(store_key, environment, review_id),
  UNIQUE(store_key, environment, acquisition_id, subject_id)
);
CREATE TABLE acquisition_invitation_subjects (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  consumed_at INTEGER,
  PRIMARY KEY(store_key, environment, acquisition_id, subject_id),
  FOREIGN KEY(store_key, environment, acquisition_id)
    REFERENCES acquisition_invitations(store_key, environment, acquisition_id)
);
