-- Withdrawal is an immutable suppression tombstone, even if its grant arrives later.
CREATE TABLE review_invitation_consent_withdrawals (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  recipient_context_hash TEXT NOT NULL CHECK(length(recipient_context_hash)=64),
  purpose TEXT NOT NULL CHECK(purpose='review_invitation'),
  evidence_reference TEXT NOT NULL CHECK(length(evidence_reference) BETWEEN 1 AND 200),
  withdrawn_at INTEGER NOT NULL CHECK(withdrawn_at > 0),
  PRIMARY KEY(store_key, environment, acquisition_id, recipient_context_hash, purpose)
);
CREATE TRIGGER review_consent_withdrawal_no_update BEFORE UPDATE ON review_invitation_consent_withdrawals
BEGIN SELECT RAISE(ABORT, 'immutable consent withdrawal'); END;
CREATE TRIGGER review_consent_withdrawal_no_delete BEFORE DELETE ON review_invitation_consent_withdrawals
BEGIN SELECT RAISE(ABORT, 'immutable consent withdrawal'); END;
CREATE TRIGGER review_consent_withdrawal_no_replace BEFORE INSERT ON review_invitation_consent_withdrawals
WHEN EXISTS (SELECT 1 FROM review_invitation_consent_withdrawals w
  WHERE w.store_key=NEW.store_key AND w.environment=NEW.environment
    AND w.acquisition_id=NEW.acquisition_id AND w.recipient_context_hash=NEW.recipient_context_hash
    AND w.purpose=NEW.purpose AND (w.evidence_reference<>NEW.evidence_reference OR w.withdrawn_at<>NEW.withdrawn_at))
BEGIN SELECT RAISE(ABORT, 'consent withdrawal conflict'); END;
