ALTER TABLE checkout_sessions ADD COLUMN settlement_state TEXT NOT NULL DEFAULT 'pending'
  CHECK(settlement_state IN ('pending', 'confirmed'));
ALTER TABLE checkout_sessions ADD COLUMN settlement_session_id TEXT;
ALTER TABLE checkout_sessions ADD COLUMN settlement_event_id TEXT;
ALTER TABLE checkout_sessions ADD COLUMN settled_at INTEGER;
ALTER TABLE checkout_sessions ADD COLUMN stripe_payment_status TEXT;
ALTER TABLE checkout_sessions ADD COLUMN stripe_amount_total_minor INTEGER;

ALTER TABLE stripe_events ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE stripe_events ADD COLUMN processing_state TEXT NOT NULL DEFAULT 'received'
  CHECK(processing_state IN ('received', 'ignored', 'confirmed', 'failed'));

CREATE UNIQUE INDEX checkout_sessions_settlement_session_id
ON checkout_sessions(settlement_session_id)
WHERE settlement_session_id IS NOT NULL;

CREATE TRIGGER checkout_session_settlement_is_monotonic
BEFORE UPDATE OF settlement_state ON checkout_sessions
WHEN NOT (OLD.settlement_state = 'pending' AND NEW.settlement_state = 'confirmed')
BEGIN
  SELECT RAISE(ABORT, 'checkout settlement transition is invalid');
END;
