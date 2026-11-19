-- The resource locator is private operator configuration; capabilities are hashed.
CREATE TABLE fulfilment_resources (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  resource_version TEXT NOT NULL,
  provider TEXT NOT NULL,
  config_ref TEXT NOT NULL,
  config_digest TEXT NOT NULL CHECK(length(config_digest) = 64),
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  PRIMARY KEY (merchant_id, environment, resource_id, resource_version)
);
CREATE TRIGGER fulfilment_resources_no_update BEFORE UPDATE ON fulfilment_resources BEGIN
  SELECT RAISE(ABORT, 'resource versions are immutable');
END;
CREATE TRIGGER fulfilment_resources_no_delete BEFORE DELETE ON fulfilment_resources BEGIN
  SELECT RAISE(ABORT, 'resource versions are immutable');
END;
CREATE TABLE fulfilment_order_bindings (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  workflow_key TEXT NOT NULL,
  workflow_digest TEXT NOT NULL,
  product_key TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  resource_version TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  PRIMARY KEY (merchant_id, environment, order_id, workflow_key, product_key, ordinal),
  FOREIGN KEY (order_id, product_key) REFERENCES order_items(order_id, product_key),
  FOREIGN KEY (merchant_id, environment, resource_id, resource_version)
    REFERENCES fulfilment_resources(merchant_id, environment, resource_id, resource_version)
);
CREATE TRIGGER fulfilment_order_bindings_before_checkout
BEFORE INSERT ON fulfilment_order_bindings WHEN EXISTS
  (SELECT 1 FROM checkout_sessions WHERE order_id = NEW.order_id) BEGIN
  SELECT RAISE(ABORT, 'fulfilment order bindings are sealed');
END;
CREATE TRIGGER fulfilment_order_bindings_no_update BEFORE UPDATE ON fulfilment_order_bindings BEGIN
  SELECT RAISE(ABORT, 'fulfilment order bindings are immutable');
END;
CREATE TRIGGER fulfilment_order_bindings_no_delete BEFORE DELETE ON fulfilment_order_bindings BEGIN
  SELECT RAISE(ABORT, 'fulfilment order bindings are immutable');
END;
CREATE TABLE fulfilment_entitlements (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  product_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  issued_at INTEGER NOT NULL,
  revoked_at INTEGER,
  expires_at INTEGER,
  PRIMARY KEY (merchant_id, environment, order_id, workflow_key, product_key, ordinal),
  FOREIGN KEY (merchant_id, environment, order_id, workflow_key, product_key, ordinal)
    REFERENCES fulfilment_order_bindings(merchant_id, environment, order_id, workflow_key, product_key, ordinal)
);
CREATE TABLE fulfilment_capabilities (
  token_digest TEXT PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  product_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  generation INTEGER NOT NULL CHECK(generation > 0),
  revoked_at INTEGER,
  UNIQUE (merchant_id, environment, order_id, workflow_key, product_key, ordinal, generation),
  FOREIGN KEY (merchant_id, environment, order_id, workflow_key, product_key, ordinal)
    REFERENCES fulfilment_entitlements(merchant_id, environment, order_id, workflow_key, product_key, ordinal)
);

-- One trusted workflow snapshot is sealed with the order, before a checkout
-- session exists. Later configuration changes cannot reinterpret it.
CREATE TABLE fulfilment_order_plans (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  actions_json TEXT NOT NULL,
  actions_digest TEXT NOT NULL CHECK(length(actions_digest) = 64)
);
CREATE TRIGGER fulfilment_order_plans_before_checkout
BEFORE INSERT ON fulfilment_order_plans WHEN EXISTS
  (SELECT 1 FROM checkout_sessions WHERE order_id = NEW.order_id) BEGIN
  SELECT RAISE(ABORT, 'fulfilment order plan is sealed');
END;
CREATE TRIGGER fulfilment_order_plans_no_update BEFORE UPDATE ON fulfilment_order_plans BEGIN
  SELECT RAISE(ABORT, 'fulfilment order plans are immutable');
END;
CREATE TRIGGER fulfilment_order_plans_no_delete BEFORE DELETE ON fulfilment_order_plans BEGIN
  SELECT RAISE(ABORT, 'fulfilment order plans are immutable');
END;
CREATE TRIGGER fulfilment_order_plans_installation_match
BEFORE INSERT ON fulfilment_order_plans WHEN NOT EXISTS
  (SELECT 1 FROM post_purchase_installation WHERE singleton=1
   AND merchant_id=NEW.merchant_id AND environment=NEW.environment) BEGIN
  SELECT RAISE(ABORT, 'fulfilment installation mismatch');
END;
CREATE TRIGGER fulfilment_order_bindings_installation_match
BEFORE INSERT ON fulfilment_order_bindings WHEN NOT EXISTS
  (SELECT 1 FROM fulfilment_order_plans WHERE order_id=NEW.order_id
   AND merchant_id=NEW.merchant_id AND environment=NEW.environment) BEGIN
  SELECT RAISE(ABORT, 'fulfilment order plan missing');
END;

-- Separate local preparation from an external send. A nonce belongs only to
-- the invocation that created the workflow claim; it is never a capability.
ALTER TABLE post_purchase_executions ADD COLUMN fulfilment_nonce TEXT;
CREATE TABLE fulfilment_notification_intents (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  claim_nonce TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared', 'sending')),
  created_at INTEGER NOT NULL,
  PRIMARY KEY (merchant_id, environment, order_id, workflow_key),
  FOREIGN KEY (merchant_id, environment, order_id, workflow_key)
    REFERENCES post_purchase_executions(merchant_id, environment, order_id, workflow_key)
);
CREATE TABLE fulfilment_recovery_audit (
  recovery_id TEXT PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment = 'test'),
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  operator TEXT NOT NULL CHECK(length(operator) BETWEEN 1 AND 128),
  evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 1 AND 512),
  recorded_at INTEGER NOT NULL,
  FOREIGN KEY (merchant_id, environment, order_id, workflow_key)
    REFERENCES post_purchase_executions(merchant_id, environment, order_id, workflow_key)
);
CREATE TRIGGER fulfilment_recovery_audit_validate BEFORE INSERT ON fulfilment_recovery_audit BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM post_purchase_executions e JOIN fulfilment_notification_intents i
    USING (merchant_id,environment,order_id,workflow_key)
    WHERE e.merchant_id=NEW.merchant_id AND e.environment=NEW.environment AND e.order_id=NEW.order_id
      AND e.workflow_key=NEW.workflow_key AND e.state='claimed' AND i.state='prepared'
      AND e.fulfilment_nonce=NEW.recovery_id AND i.claim_nonce=NEW.recovery_id
  ) THEN RAISE(ABORT, 'fulfilment recovery claim mismatch') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM fulfilment_entitlements e
    WHERE e.merchant_id=NEW.merchant_id AND e.environment=NEW.environment AND e.order_id=NEW.order_id
      AND e.workflow_key=NEW.workflow_key AND e.revoked_at IS NULL
      AND (e.expires_at IS NULL OR e.expires_at>NEW.recorded_at)
      AND (SELECT COUNT(*) FROM fulfilment_capabilities c WHERE c.merchant_id=e.merchant_id
       AND c.environment=e.environment AND c.order_id=e.order_id AND c.workflow_key=e.workflow_key
       AND c.product_key=e.product_key AND c.ordinal=e.ordinal AND c.revoked_at IS NULL) != 1
  ) THEN RAISE(ABORT, 'fulfilment recovery capabilities incomplete') END;
END;
CREATE TRIGGER fulfilment_recovery_audit_no_update BEFORE UPDATE ON fulfilment_recovery_audit BEGIN
  SELECT RAISE(ABORT, 'fulfilment recovery audit is immutable');
END;
CREATE TRIGGER fulfilment_recovery_audit_no_delete BEFORE DELETE ON fulfilment_recovery_audit BEGIN
  SELECT RAISE(ABORT, 'fulfilment recovery audit is immutable');
END;
-- A preparation proves no send has started. It must use the dedicated
-- prepared-recovery path, not the generic ambiguous-send disposition.
CREATE TRIGGER fulfilment_prepared_not_ambiguous
BEFORE INSERT ON post_purchase_recovery_audit WHEN EXISTS (
  SELECT 1 FROM fulfilment_notification_intents i
  WHERE i.merchant_id=NEW.merchant_id AND i.environment=NEW.environment
    AND i.order_id=NEW.order_id AND i.workflow_key=NEW.workflow_key AND i.state='prepared'
) BEGIN
  SELECT RAISE(ABORT, 'prepared fulfilment requires prepared recovery');
END;
CREATE TABLE fulfilment_preparation_failures (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  failure_code TEXT NOT NULL,
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY (merchant_id, environment, order_id, workflow_key),
  FOREIGN KEY (merchant_id, environment, order_id, workflow_key)
    REFERENCES post_purchase_executions(merchant_id, environment, order_id, workflow_key)
);
CREATE TRIGGER fulfilment_preparation_failures_no_update BEFORE UPDATE ON fulfilment_preparation_failures BEGIN
  SELECT RAISE(ABORT, 'fulfilment preparation failure is immutable');
END;
CREATE TRIGGER fulfilment_preparation_failures_no_delete BEFORE DELETE ON fulfilment_preparation_failures BEGIN
  SELECT RAISE(ABORT, 'fulfilment preparation failure is immutable');
END;
CREATE TABLE fulfilment_order_holds (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  failure_code TEXT NOT NULL,
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY (merchant_id, environment, order_id)
);
CREATE TRIGGER fulfilment_order_holds_no_update BEFORE UPDATE ON fulfilment_order_holds BEGIN
  SELECT RAISE(ABORT, 'fulfilment order hold is immutable');
END;
CREATE TRIGGER fulfilment_order_holds_no_delete BEFORE DELETE ON fulfilment_order_holds BEGIN
  SELECT RAISE(ABORT, 'fulfilment order hold is immutable');
END;
CREATE TRIGGER fulfilment_entitlements_monotonic BEFORE UPDATE ON fulfilment_entitlements
WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR OLD.merchant_id != NEW.merchant_id
  OR OLD.environment != NEW.environment OR OLD.order_id != NEW.order_id OR OLD.workflow_key != NEW.workflow_key
  OR OLD.product_key != NEW.product_key OR OLD.ordinal != NEW.ordinal OR OLD.issued_at != NEW.issued_at
  OR OLD.expires_at IS NOT NEW.expires_at BEGIN
  SELECT RAISE(ABORT, 'fulfilment entitlement can only be revoked once');
END;
CREATE TRIGGER fulfilment_entitlements_no_delete BEFORE DELETE ON fulfilment_entitlements BEGIN
  SELECT RAISE(ABORT, 'fulfilment entitlement is immutable');
END;
CREATE TABLE fulfilment_revocation_audit (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  order_id TEXT NOT NULL,
  workflow_key TEXT NOT NULL,
  product_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  operator TEXT NOT NULL CHECK(length(operator) BETWEEN 1 AND 128),
  evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 1 AND 512),
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY (merchant_id,environment,order_id,workflow_key,product_key,ordinal),
  FOREIGN KEY (merchant_id,environment,order_id,workflow_key,product_key,ordinal)
    REFERENCES fulfilment_entitlements(merchant_id,environment,order_id,workflow_key,product_key,ordinal)
);
CREATE TRIGGER fulfilment_revocation_validate BEFORE INSERT ON fulfilment_revocation_audit BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM fulfilment_entitlements e JOIN post_purchase_installation i
      ON i.singleton=1 AND i.merchant_id=e.merchant_id AND i.environment=e.environment
    WHERE e.merchant_id=NEW.merchant_id AND e.environment=NEW.environment AND e.order_id=NEW.order_id
      AND e.workflow_key=NEW.workflow_key AND e.product_key=NEW.product_key AND e.ordinal=NEW.ordinal
      AND e.revoked_at IS NULL
  ) THEN RAISE(ABORT, 'fulfilment entitlement missing or already revoked') END;
END;
CREATE TRIGGER fulfilment_revocation_apply AFTER INSERT ON fulfilment_revocation_audit BEGIN
  UPDATE fulfilment_entitlements SET revoked_at=NEW.recorded_at
  WHERE merchant_id=NEW.merchant_id AND environment=NEW.environment AND order_id=NEW.order_id
    AND workflow_key=NEW.workflow_key AND product_key=NEW.product_key AND ordinal=NEW.ordinal;
END;
CREATE TRIGGER fulfilment_revocation_audit_no_update BEFORE UPDATE ON fulfilment_revocation_audit BEGIN
  SELECT RAISE(ABORT, 'fulfilment revocation audit is immutable');
END;
CREATE TRIGGER fulfilment_revocation_audit_no_delete BEFORE DELETE ON fulfilment_revocation_audit BEGIN
  SELECT RAISE(ABORT, 'fulfilment revocation audit is immutable');
END;
