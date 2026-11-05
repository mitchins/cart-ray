DROP INDEX invitation_generation_pending;
ALTER TABLE invitation_generations RENAME TO invitation_generations_previous;
CREATE TABLE invitation_generations (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('test','live')),
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL CHECK(length(generation_id)=64),
  request_key TEXT NOT NULL,
  token_hash TEXT NOT NULL CHECK(length(token_hash)=64),
  key_version TEXT NOT NULL,
  expected_token_hash TEXT NOT NULL CHECK(length(expected_token_hash)=64),
  expected_revoked INTEGER NOT NULL CHECK(expected_revoked IN (0,1)),
  expected_revocation_epoch INTEGER NOT NULL,
  delivery_identity TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','dispatching','accepted','failed','uncertain','revoked')),
  prepared_at INTEGER NOT NULL,
  accepted_at INTEGER,
  expires_at INTEGER,
  PRIMARY KEY(store_key, environment, acquisition_id, generation_id),
  UNIQUE(store_key, environment, request_key),
  UNIQUE(store_key, environment, token_hash),
  UNIQUE(store_key, environment, delivery_identity),
  FOREIGN KEY(store_key, environment, acquisition_id)
    REFERENCES acquisition_invitations(store_key, environment, acquisition_id)
);
INSERT INTO invitation_generations SELECT * FROM invitation_generations_previous;
DROP TABLE invitation_generations_previous;
CREATE UNIQUE INDEX invitation_generation_pending ON invitation_generations(store_key, environment, acquisition_id)
WHERE state IN ('prepared','dispatching','uncertain');
