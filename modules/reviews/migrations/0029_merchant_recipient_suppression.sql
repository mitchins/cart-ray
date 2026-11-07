-- Test-only pseudonymous, merchant-scoped review-request suppression.
CREATE TABLE review_recipient_suppressions (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  recipient_context_hash TEXT NOT NULL CHECK(length(recipient_context_hash)=64),
  purpose TEXT NOT NULL CHECK(purpose='review_invitation'),
  created_at INTEGER NOT NULL CHECK(created_at>0),
  PRIMARY KEY(store_key,environment,recipient_context_hash,purpose)
);
CREATE TRIGGER review_recipient_suppression_no_update BEFORE UPDATE ON review_recipient_suppressions
BEGIN SELECT RAISE(ABORT, 'recipient suppression immutable'); END;
CREATE TRIGGER review_recipient_suppression_no_delete BEFORE DELETE ON review_recipient_suppressions
BEGIN SELECT RAISE(ABORT, 'recipient suppression immutable'); END;
