CREATE TABLE post_purchase_installation (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL
);
CREATE TRIGGER post_purchase_installation_no_update BEFORE UPDATE ON post_purchase_installation BEGIN
  SELECT RAISE(ABORT, 'post-purchase installation is immutable');
END;
CREATE TRIGGER post_purchase_installation_no_delete BEFORE DELETE ON post_purchase_installation BEGIN
  SELECT RAISE(ABORT, 'post-purchase installation is immutable');
END;

CREATE TABLE post_purchase_executions (
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  workflow_key TEXT NOT NULL,
  action_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('claimed', 'accepted', 'uncertain')),
  provider_reference TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (merchant_id, environment, order_id, workflow_key)
);
