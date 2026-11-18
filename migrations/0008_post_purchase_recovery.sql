-- Operator-only disposition of a stale, potentially delivered execution.
-- The insert and state transition are one SQLite statement via the trigger.
CREATE TABLE post_purchase_recovery_audit (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment = 'test'),
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  action_digest TEXT NOT NULL,
  operator TEXT NOT NULL CHECK(length(operator) BETWEEN 1 AND 128),
  evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 1 AND 512),
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY (merchant_id, environment, order_id, workflow_key),
  FOREIGN KEY (merchant_id, environment, order_id, workflow_key)
    REFERENCES post_purchase_executions(merchant_id, environment, order_id, workflow_key)
);

CREATE TRIGGER post_purchase_recovery_validate BEFORE INSERT ON post_purchase_recovery_audit BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM post_purchase_executions e
    JOIN post_purchase_installation i ON i.singleton=1 AND i.merchant_id=e.merchant_id
      AND i.environment=e.environment
    WHERE e.merchant_id=NEW.merchant_id AND e.environment=NEW.environment
      AND e.order_id=NEW.order_id AND e.workflow_key=NEW.workflow_key
      AND e.action_digest=NEW.action_digest AND e.state='claimed'
      AND e.updated_at <= CAST(strftime('%s','now') AS INTEGER)-1200
      AND NEW.recorded_at = CAST(strftime('%s','now') AS INTEGER)
  ) THEN RAISE(ABORT, 'claim missing, active, mismatched or not old enough') END;
END;

CREATE TRIGGER post_purchase_recovery_apply AFTER INSERT ON post_purchase_recovery_audit BEGIN
  UPDATE post_purchase_executions SET state='uncertain',updated_at=NEW.recorded_at
   WHERE merchant_id=NEW.merchant_id AND environment=NEW.environment
     AND order_id=NEW.order_id AND workflow_key=NEW.workflow_key
     AND action_digest=NEW.action_digest AND state='claimed';
END;

CREATE TRIGGER post_purchase_recovery_no_update BEFORE UPDATE ON post_purchase_recovery_audit BEGIN
  SELECT RAISE(ABORT, 'recovery audit is immutable');
END;
CREATE TRIGGER post_purchase_recovery_no_delete BEFORE DELETE ON post_purchase_recovery_audit BEGIN
  SELECT RAISE(ABORT, 'recovery audit is immutable');
END;
