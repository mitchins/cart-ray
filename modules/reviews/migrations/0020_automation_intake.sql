-- Test-only acquisition intake; no dispatcher or email capability.
CREATE TABLE review_automation_activation (
  store_key TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment='test'),
  activation_epoch INTEGER NOT NULL CHECK(activation_epoch > 0),
  outbox_watermark INTEGER NOT NULL CHECK(outbox_watermark >= 0),
  delay_seconds INTEGER NOT NULL CHECK(delay_seconds >= 0),
  max_lateness INTEGER NOT NULL CHECK(max_lateness >= 0),
  policy_version TEXT NOT NULL,
  PRIMARY KEY(store_key, environment)
);
CREATE TRIGGER review_automation_activation_immutable_update BEFORE UPDATE ON review_automation_activation
BEGIN SELECT RAISE(ABORT, 'immutable activation'); END;
CREATE TRIGGER review_automation_activation_immutable_delete BEFORE DELETE ON review_automation_activation
BEGIN SELECT RAISE(ABORT, 'immutable activation'); END;
CREATE TRIGGER review_automation_activation_immutable_replace BEFORE INSERT ON review_automation_activation
WHEN EXISTS (SELECT 1 FROM review_automation_activation a
             WHERE a.store_key=NEW.store_key AND a.environment=NEW.environment)
BEGIN SELECT RAISE(ABORT, 'immutable activation'); END;
CREATE TABLE review_automation_intake (
  store_key TEXT NOT NULL, environment TEXT NOT NULL,
  outbox_id INTEGER NOT NULL, acquisition_id TEXT NOT NULL,
  evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
  confirmed_at INTEGER NOT NULL, due_at INTEGER NOT NULL,
  policy_version TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN ('accepted','pre_activation','late')),
  PRIMARY KEY(store_key, environment, outbox_id),
  UNIQUE(store_key, environment, acquisition_id)
);
CREATE TRIGGER review_automation_intake_conflict BEFORE INSERT ON review_automation_intake
WHEN EXISTS (SELECT 1 FROM review_automation_intake i
  WHERE i.store_key=NEW.store_key AND i.environment=NEW.environment
    AND (i.outbox_id=NEW.outbox_id OR i.acquisition_id=NEW.acquisition_id)
    AND (i.outbox_id<>NEW.outbox_id OR i.acquisition_id<>NEW.acquisition_id
      OR i.evidence_hash<>NEW.evidence_hash OR i.policy_version<>NEW.policy_version))
BEGIN SELECT RAISE(ABORT, 'intake identity conflict'); END;
CREATE TABLE review_automation_intents (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, acquisition_id TEXT NOT NULL,
  outbox_id INTEGER NOT NULL, due_at INTEGER NOT NULL, policy_version TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state='held_recipient_consent'),
  PRIMARY KEY(store_key, environment, acquisition_id),
  FOREIGN KEY(store_key,environment,outbox_id) REFERENCES review_automation_intake(store_key,environment,outbox_id)
);
