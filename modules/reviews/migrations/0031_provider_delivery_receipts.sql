-- Provider acceptance identity and immutable, independently sourced activity.
CREATE TABLE review_provider_deliveries (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  delivery_identity TEXT NOT NULL,
  provider TEXT NOT NULL CHECK(provider='smtp2go'),
  provider_email_id TEXT NOT NULL CHECK(length(provider_email_id) BETWEEN 1 AND 128),
  accepted_at INTEGER NOT NULL CHECK(accepted_at>0),
  PRIMARY KEY(store_key,environment,acquisition_id,generation_id),
  UNIQUE(store_key,environment,provider,provider_email_id),
  UNIQUE(store_key,environment,delivery_identity),
  FOREIGN KEY(store_key,environment,acquisition_id,generation_id)
    REFERENCES invitation_generations(store_key,environment,acquisition_id,generation_id)
);
CREATE TRIGGER provider_delivery_no_update BEFORE UPDATE ON review_provider_deliveries
BEGIN SELECT RAISE(ABORT, 'provider delivery immutable'); END;
CREATE TRIGGER provider_delivery_no_delete BEFORE DELETE ON review_provider_deliveries
BEGIN SELECT RAISE(ABORT, 'provider delivery immutable'); END;
CREATE TABLE review_provider_activity (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  provider_email_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('processed','delivered','rejected','soft-bounced','hard-bounced')),
  event_at TEXT NOT NULL CHECK(length(event_at) BETWEEN 20 AND 40),
  observed_at INTEGER NOT NULL CHECK(observed_at>0),
  PRIMARY KEY(store_key,environment,provider_email_id,event_type,event_at),
  FOREIGN KEY(store_key,environment,acquisition_id,generation_id)
    REFERENCES review_provider_deliveries(store_key,environment,acquisition_id,generation_id)
);
CREATE TRIGGER provider_activity_no_update BEFORE UPDATE ON review_provider_activity
BEGIN SELECT RAISE(ABORT, 'provider activity immutable'); END;
CREATE TRIGGER provider_activity_no_delete BEFORE DELETE ON review_provider_activity
BEGIN SELECT RAISE(ABORT, 'provider activity immutable'); END;
-- A provider event can correlate a lost send response without inventing acceptance time.
CREATE TABLE review_provider_identity_discoveries (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  delivery_identity TEXT NOT NULL,
  provider_email_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('processed','delivered','rejected','soft-bounced','hard-bounced')),
  event_at TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  PRIMARY KEY(store_key,environment,acquisition_id,generation_id,provider_email_id,event_type,event_at),
  FOREIGN KEY(store_key,environment,acquisition_id,generation_id)
    REFERENCES invitation_generations(store_key,environment,acquisition_id,generation_id)
);
CREATE TRIGGER provider_discovery_no_update BEFORE UPDATE ON review_provider_identity_discoveries
BEGIN SELECT RAISE(ABORT, 'provider discovery immutable'); END;
CREATE TRIGGER provider_discovery_no_delete BEFORE DELETE ON review_provider_identity_discoveries
BEGIN SELECT RAISE(ABORT, 'provider discovery immutable'); END;
