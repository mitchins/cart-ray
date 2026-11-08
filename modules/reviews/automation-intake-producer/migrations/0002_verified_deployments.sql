-- Append-only deployment verification records; application authorization must restrict writes.
CREATE TABLE reviews_verified_deployment (
  subscriber_id TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  account_id TEXT NOT NULL,
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment = 'test'),
  source_database_id TEXT NOT NULL,
  stripe_account_id TEXT NOT NULL,
  catalogue_version TEXT NOT NULL,
  mapping_sha256 TEXT NOT NULL,
  source_version TEXT NOT NULL,
  producer_version TEXT NOT NULL,
  reviews_version TEXT NOT NULL,
  consumer_version TEXT NOT NULL,
  authority_version TEXT NOT NULL,
  context_database_id TEXT NOT NULL,
  event_queue_id TEXT NOT NULL,
  dead_letter_queue_id TEXT NOT NULL,
  source_worker TEXT NOT NULL,
  producer_worker TEXT NOT NULL,
  reviews_worker TEXT NOT NULL,
  consumer_worker TEXT NOT NULL,
  authority_worker TEXT NOT NULL,
  event_queue_name TEXT NOT NULL,
  dead_letter_queue_name TEXT NOT NULL,
  activation_status TEXT NOT NULL CHECK (activation_status IN ('verified','active')),
  verified_at INTEGER NOT NULL CHECK (verified_at > 0),
  PRIMARY KEY (subscriber_id, installation_id, revision)
);
CREATE TRIGGER reviews_verified_deployment_no_update BEFORE UPDATE ON reviews_verified_deployment
BEGIN SELECT RAISE(ABORT, 'verification records are immutable'); END;
CREATE TRIGGER reviews_verified_deployment_no_delete BEFORE DELETE ON reviews_verified_deployment
BEGIN SELECT RAISE(ABORT, 'verification records are immutable'); END;
