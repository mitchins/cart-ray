-- Staff investigation is evidence, never an instruction to resend.
CREATE TABLE review_provider_operations_audit (
  event_id TEXT PRIMARY KEY CHECK(length(event_id)=64),
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  delivery_identity TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('refresh_requested','evidence_acknowledged')),
  provider_email_id TEXT,
  event_type TEXT,
  event_at TEXT,
  occurred_at INTEGER NOT NULL,
  FOREIGN KEY(store_key,environment,acquisition_id,generation_id)
    REFERENCES invitation_generations(store_key,environment,acquisition_id,generation_id)
);
CREATE TRIGGER provider_operations_no_update BEFORE UPDATE ON review_provider_operations_audit
BEGIN SELECT RAISE(ABORT, 'provider operations immutable'); END;
CREATE TRIGGER provider_operations_no_delete BEFORE DELETE ON review_provider_operations_audit
BEGIN SELECT RAISE(ABORT, 'provider operations immutable'); END;
CREATE INDEX provider_operations_delivery ON review_provider_operations_audit(store_key,environment,delivery_identity,occurred_at);
CREATE UNIQUE INDEX provider_operations_ack_once ON review_provider_operations_audit
(store_key,environment,delivery_identity,actor,provider_email_id,event_type,event_at)
WHERE action='evidence_acknowledged';
