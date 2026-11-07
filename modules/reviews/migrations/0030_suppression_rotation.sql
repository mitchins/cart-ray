-- Test-only, store-scoped rotation fence and pseudonym alias graph. Raw email and
-- secret key bytes are never stored in Reviews D1.
CREATE TABLE review_suppression_rotation (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  state TEXT NOT NULL CHECK(state IN ('frozen','active')),
  active_binding_version TEXT NOT NULL,
  target_binding_version TEXT,
  old_key_check TEXT,
  target_key_check TEXT,
  PRIMARY KEY(store_key,environment)
);
CREATE TABLE review_suppression_aliases (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  recipient_context_hash TEXT NOT NULL CHECK(length(recipient_context_hash)=64),
  canonical_hash TEXT NOT NULL CHECK(length(canonical_hash)=64),
  binding_version TEXT NOT NULL,
  PRIMARY KEY(store_key,environment,recipient_context_hash),
  UNIQUE(store_key,environment,canonical_hash,binding_version)
);
CREATE INDEX review_suppression_alias_group ON review_suppression_aliases(store_key,environment,canonical_hash);
CREATE TRIGGER review_suppression_alias_no_update BEFORE UPDATE ON review_suppression_aliases
BEGIN SELECT RAISE(ABORT, 'suppression alias immutable'); END;
CREATE TRIGGER review_suppression_alias_no_delete BEFORE DELETE ON review_suppression_aliases
BEGIN SELECT RAISE(ABORT, 'suppression alias immutable'); END;
-- Fan out even when an older already-running Worker inserts the tombstone.
CREATE TRIGGER review_suppression_alias_fanout AFTER INSERT ON review_recipient_suppressions
BEGIN
  INSERT OR IGNORE INTO review_recipient_suppressions
    (store_key,environment,recipient_context_hash,purpose,created_at)
  SELECT a.store_key,a.environment,a.recipient_context_hash,NEW.purpose,NEW.created_at
  FROM review_suppression_aliases a JOIN review_suppression_aliases b
  USING(store_key,environment,canonical_hash)
  WHERE b.store_key=NEW.store_key AND b.environment=NEW.environment
    AND b.recipient_context_hash=NEW.recipient_context_hash;
END;
-- The database enforces the fence even for already-running older Worker code.
CREATE TRIGGER review_suppression_fence_binding BEFORE INSERT ON review_automation_recipient_bindings
WHEN EXISTS (SELECT 1 FROM review_suppression_rotation r WHERE r.store_key=NEW.store_key
  AND r.environment=NEW.environment AND (r.state='frozen' OR r.active_binding_version<>NEW.binding_version))
BEGIN SELECT RAISE(ABORT, 'suppression rotation fenced'); END;
CREATE TRIGGER review_suppression_fence_dispatch BEFORE UPDATE OF state ON invitation_generations
WHEN NEW.state='dispatching' AND OLD.state<>'dispatching' AND EXISTS (
  SELECT 1 FROM review_suppression_rotation r WHERE r.store_key=NEW.store_key
  AND r.environment=NEW.environment AND r.state='frozen')
BEGIN SELECT RAISE(ABORT, 'suppression rotation fenced'); END;
