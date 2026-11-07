-- The initial merchant-scoped suppression key is pinned independently of Worker
-- configuration. Rotation requires a later, explicitly qualified alias migration.
CREATE TABLE review_suppression_key_registry (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment = 'test'),
  installation_id TEXT NOT NULL,
  key_version TEXT NOT NULL,
  key_check TEXT NOT NULL CHECK (length(key_check) = 64),
  PRIMARY KEY (store_key, environment, installation_id)
);
CREATE TRIGGER review_suppression_key_registry_no_update
BEFORE UPDATE ON review_suppression_key_registry
BEGIN SELECT RAISE(ABORT, 'suppression key pinned'); END;
CREATE TRIGGER review_suppression_key_registry_no_delete
BEFORE DELETE ON review_suppression_key_registry
BEGIN SELECT RAISE(ABORT, 'suppression key pinned'); END;
