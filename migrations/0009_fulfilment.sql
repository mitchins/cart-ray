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
