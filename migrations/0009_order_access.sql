-- One non-expiring, revocable order-access capability per merchant and order.
-- Resource membership remains the immutable purchased-item snapshot.
CREATE TABLE order_access (
  merchant TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  token_digest TEXT NOT NULL UNIQUE,
  revoked_at INTEGER,
  PRIMARY KEY (merchant, environment, order_id)
);
