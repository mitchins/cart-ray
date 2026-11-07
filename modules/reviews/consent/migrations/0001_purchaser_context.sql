CREATE TABLE purchaser_contexts (
  token_hash TEXT PRIMARY KEY,
  checkout_session_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  CHECK(expires_at > created_at)
);
CREATE INDEX purchaser_context_expiry ON purchaser_contexts(expires_at);
CREATE TABLE purchaser_email_challenges (
  token_hash TEXT PRIMARY KEY REFERENCES purchaser_contexts(token_hash),
  event_id TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  source_provenance TEXT NOT NULL,
  recipient_context_hash TEXT NOT NULL,
  binding_version TEXT NOT NULL,
  code_digest TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
  delivery_state TEXT NOT NULL CHECK(delivery_state IN ('claimed','sent','uncertain')),
  verified_at INTEGER
);
CREATE TABLE purchaser_consent_grants (
  event_id TEXT PRIMARY KEY,
  acquisition_id TEXT NOT NULL,
  source_provenance TEXT NOT NULL,
  recipient_context_hash TEXT NOT NULL,
  binding_version TEXT NOT NULL,
  evidence_reference TEXT NOT NULL UNIQUE,
  granted_at INTEGER NOT NULL
);
CREATE TRIGGER purchaser_grants_no_update BEFORE UPDATE ON purchaser_consent_grants
BEGIN SELECT RAISE(ABORT, 'grants immutable'); END;
CREATE TRIGGER purchaser_grants_no_delete BEFORE DELETE ON purchaser_consent_grants
BEGIN SELECT RAISE(ABORT, 'grants immutable'); END;
