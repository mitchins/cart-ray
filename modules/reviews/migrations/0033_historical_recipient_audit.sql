-- Private historical recipient lookup, not authority to send.
CREATE TABLE review_historical_recipient_audit (
  event_id TEXT PRIMARY KEY CHECK(length(event_id)=64),
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  delivery_identity TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  FOREIGN KEY(store_key,environment,acquisition_id,generation_id)
    REFERENCES invitation_generations(store_key,environment,acquisition_id,generation_id)
);
CREATE TRIGGER historical_recipient_no_update BEFORE UPDATE ON review_historical_recipient_audit
BEGIN SELECT RAISE(ABORT, 'historical recipient audit immutable'); END;
CREATE TRIGGER historical_recipient_no_delete BEFORE DELETE ON review_historical_recipient_audit
BEGIN SELECT RAISE(ABORT, 'historical recipient audit immutable'); END;
