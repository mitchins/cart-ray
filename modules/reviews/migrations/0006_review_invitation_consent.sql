-- Receipt of consent is independent of purchase settlement. Rows are supplied
-- by a trusted consent adapter; a payment event cannot create consent.
CREATE TABLE review_invitation_consents (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  recipient_context_hash TEXT NOT NULL CHECK(length(recipient_context_hash) = 64),
  purpose TEXT NOT NULL CHECK(purpose = 'review_invitation'),
  evidence_reference TEXT NOT NULL CHECK(length(evidence_reference) BETWEEN 1 AND 200),
  granted_at INTEGER NOT NULL,
  revoked_at INTEGER,
  PRIMARY KEY(store_key, environment, acquisition_id)
);
