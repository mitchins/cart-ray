CREATE TABLE checkout_reconciliations (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  session_id TEXT NOT NULL UNIQUE,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  next_attempt_at INTEGER NOT NULL,
  lease_token TEXT,
  lease_expires_at INTEGER,
  last_attempt_at INTEGER,
  last_outcome TEXT CHECK(last_outcome IN (
    'open', 'unsettled', 'rejected', 'transient_error', 'confirmed', 'expired'
  )),
  last_error TEXT CHECK(last_error IN (
    'stripe_retrieval_failed', 'projection_rejected', 'reconciliation_rejected',
    'reconciliation_lookup_failed', 'checkout_not_settled', 'unrecognized_checkout_state'
  )),
  observed_status TEXT,
  observed_payment_status TEXT,
  observed_amount_total_minor INTEGER,
  updated_at INTEGER NOT NULL
);

CREATE INDEX checkout_sessions_reconciliation_candidates
ON checkout_sessions(settlement_state, updated_at, external_session_id);

CREATE INDEX checkout_reconciliations_due
ON checkout_reconciliations(next_attempt_at, lease_expires_at, order_id);
