PRAGMA foreign_keys = ON;

CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  checkout_request_id TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  manifest_version TEXT NOT NULL,
  items_digest TEXT NOT NULL,
  currency TEXT NOT NULL,
  subtotal_minor INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE order_items (
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  product_key TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  stripe_price_id TEXT NOT NULL,
  unit_amount_minor INTEGER NOT NULL,
  fulfilment_resources_json TEXT NOT NULL,
  PRIMARY KEY (order_id, product_key)
);

CREATE TABLE checkout_sessions (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  state TEXT NOT NULL CHECK(state IN ('creating', 'session_created', 'sealed', 'redirect_issued', 'expired')),
  external_session_id TEXT UNIQUE,
  redirect_url TEXT,
  projection_nonce TEXT NOT NULL,
  lease_expires_at INTEGER,
  creation_attempt INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL
);

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,
  UNIQUE(order_id, event_type)
);

CREATE TABLE stripe_events (
  stripe_event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  session_id TEXT,
  payload_json TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  processed_at INTEGER,
  processing_error TEXT
);

CREATE INDEX checkout_sessions_external_session_id ON checkout_sessions(external_session_id);
CREATE INDEX outbox_undelivered ON outbox(delivered_at, id);

CREATE TRIGGER orders_are_immutable
BEFORE UPDATE ON orders
BEGIN
  SELECT RAISE(ABORT, 'orders are immutable');
END;

CREATE TRIGGER order_items_are_immutable
BEFORE UPDATE ON order_items
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;

CREATE TRIGGER order_items_cannot_be_deleted
BEFORE DELETE ON order_items
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;

CREATE TRIGGER order_items_cannot_be_added_to_sealed_order
BEFORE INSERT ON order_items
WHEN EXISTS (SELECT 1 FROM checkout_sessions WHERE order_id = NEW.order_id)
BEGIN
  SELECT RAISE(ABORT, 'order_items are immutable');
END;
