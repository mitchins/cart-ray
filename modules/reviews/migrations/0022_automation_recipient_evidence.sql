-- Synthetic-only, purpose-specific evidence; no address, send capability or scheduler.
CREATE TABLE review_automation_recipient_bindings (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL,
  outbox_id INTEGER NOT NULL,
  recipient_context_hash TEXT NOT NULL CHECK(length(recipient_context_hash)=64),
  binding_version TEXT NOT NULL CHECK(length(binding_version) BETWEEN 1 AND 128),
  evidence_reference TEXT NOT NULL CHECK(length(evidence_reference) BETWEEN 1 AND 200),
  verified_at INTEGER NOT NULL CHECK(verified_at > 0),
  PRIMARY KEY(store_key, environment, acquisition_id),
  UNIQUE(store_key, environment, outbox_id),
  FOREIGN KEY(store_key,environment,acquisition_id)
    REFERENCES review_automation_intents(store_key,environment,acquisition_id)
);
CREATE TRIGGER review_automation_recipient_binding_conflict BEFORE INSERT ON review_automation_recipient_bindings
WHEN EXISTS (SELECT 1 FROM review_automation_recipient_bindings b
  WHERE b.store_key=NEW.store_key AND b.environment=NEW.environment
    AND (b.outbox_id=NEW.outbox_id OR b.acquisition_id=NEW.acquisition_id)
    AND (b.outbox_id<>NEW.outbox_id OR b.acquisition_id<>NEW.acquisition_id
      OR b.recipient_context_hash<>NEW.recipient_context_hash OR b.binding_version<>NEW.binding_version
      OR b.evidence_reference<>NEW.evidence_reference OR b.verified_at<>NEW.verified_at))
BEGIN SELECT RAISE(ABORT, 'recipient evidence conflict'); END;
CREATE TRIGGER review_automation_recipient_binding_no_update BEFORE UPDATE ON review_automation_recipient_bindings
BEGIN SELECT RAISE(ABORT, 'immutable recipient evidence'); END;
CREATE TRIGGER review_automation_recipient_binding_no_delete BEFORE DELETE ON review_automation_recipient_bindings
BEGIN SELECT RAISE(ABORT, 'immutable recipient evidence'); END;
