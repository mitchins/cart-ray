PRAGMA foreign_keys = OFF;

DROP TRIGGER order_items_cannot_be_added_to_sealed_order;

CREATE TABLE checkout_sessions_next (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  state TEXT NOT NULL CHECK(state IN ('creating', 'session_created', 'sealed', 'redirect_issued', 'expired')),
  external_session_id TEXT UNIQUE,
  redirect_url TEXT,
  projection_nonce TEXT NOT NULL,
  lease_expires_at INTEGER,
  creation_attempt INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL,
  settlement_state TEXT NOT NULL DEFAULT 'pending' CHECK(settlement_state IN ('pending', 'confirmed', 'expired')),
  settlement_session_id TEXT,
  settlement_event_id TEXT,
  settled_at INTEGER,
  stripe_payment_status TEXT,
  stripe_amount_total_minor INTEGER,
  expiration_event_id TEXT,
  expired_at INTEGER
);

INSERT INTO checkout_sessions_next (
  order_id, state, external_session_id, redirect_url, projection_nonce, lease_expires_at, creation_attempt,
  updated_at, settlement_state, settlement_session_id, settlement_event_id, settled_at,
  stripe_payment_status, stripe_amount_total_minor
)
SELECT
  order_id, state, external_session_id, redirect_url, projection_nonce, lease_expires_at, creation_attempt,
  updated_at, settlement_state, settlement_session_id, settlement_event_id, settled_at,
  stripe_payment_status, stripe_amount_total_minor
FROM checkout_sessions;

DROP TABLE checkout_sessions;
ALTER TABLE checkout_sessions_next RENAME TO checkout_sessions;

CREATE UNIQUE INDEX checkout_sessions_settlement_session_id
ON checkout_sessions(settlement_session_id)
WHERE settlement_session_id IS NOT NULL;

CREATE TRIGGER checkout_redirect_issued_outbox
AFTER UPDATE OF state ON checkout_sessions
WHEN NEW.state = 'redirect_issued' AND OLD.redirect_url IS NULL
BEGIN
  INSERT OR IGNORE INTO outbox(order_id, event_type, payload_json, created_at)
  VALUES (NEW.order_id, 'CheckoutRedirectIssued',
          json_object('order_id', NEW.order_id, 'session_id', NEW.external_session_id), NEW.updated_at);
END;

CREATE TRIGGER order_items_cannot_be_added_to_sealed_order
BEFORE INSERT ON order_items
WHEN EXISTS (SELECT 1 FROM checkout_sessions WHERE order_id = NEW.order_id)
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;

CREATE TRIGGER checkout_session_settlement_is_monotonic
BEFORE UPDATE OF settlement_state ON checkout_sessions
WHEN NOT (
  OLD.settlement_state = 'pending' AND NEW.settlement_state IN ('confirmed', 'expired')
)
BEGIN
  SELECT RAISE(ABORT, 'checkout settlement transition is invalid');
END;

CREATE TABLE stripe_events_next (
  stripe_event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  session_id TEXT,
  payload_json TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  processed_at INTEGER,
  processing_error TEXT,
  payload_sha256 TEXT NOT NULL DEFAULT '',
  processing_state TEXT NOT NULL DEFAULT 'received'
    CHECK(processing_state IN ('received', 'ignored', 'confirmed', 'expired', 'failed'))
);

INSERT INTO stripe_events_next (
  stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
  processing_error, payload_sha256, processing_state
)
SELECT
  stripe_event_id, event_type, session_id, payload_json, received_at, processed_at,
  processing_error, payload_sha256, processing_state
FROM stripe_events;

DROP TABLE stripe_events;
ALTER TABLE stripe_events_next RENAME TO stripe_events;

PRAGMA foreign_keys = ON;
