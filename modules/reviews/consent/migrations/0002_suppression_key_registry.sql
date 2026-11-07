-- The initial merchant-scoped suppression key is pinned independently of Worker
-- configuration. Rotation requires a later, explicitly qualified alias migration.
CREATE TABLE review_suppression_key_registry (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment = 'test'),
  installation_id TEXT NOT NULL,
  key_version TEXT NOT NULL,
  key_check TEXT NOT NULL CHECK (length(key_check) = 64),
  PRIMARY KEY (store_key, environment, installation_id, key_version)
);
CREATE TABLE review_suppression_key_current (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  installation_id TEXT NOT NULL,
  key_version TEXT NOT NULL,
  PRIMARY KEY (store_key, environment, installation_id),
  FOREIGN KEY (store_key, environment, installation_id, key_version)
    REFERENCES review_suppression_key_registry(store_key, environment, installation_id, key_version)
);
CREATE TABLE review_suppression_key_guard (ok INTEGER NOT NULL CHECK(ok=1));
CREATE TRIGGER review_suppression_key_current_guard BEFORE UPDATE ON review_suppression_key_current
WHEN NOT EXISTS (SELECT 1 FROM review_suppression_key_guard WHERE ok=1)
BEGIN SELECT RAISE(ABORT, 'suppression key switch requires guarded rotation'); END;
CREATE TRIGGER review_suppression_key_current_no_delete BEFORE DELETE ON review_suppression_key_current
BEGIN SELECT RAISE(ABORT, 'suppression key current cannot be deleted'); END;
CREATE TRIGGER review_suppression_key_registry_no_update
BEFORE UPDATE ON review_suppression_key_registry
BEGIN SELECT RAISE(ABORT, 'suppression key pinned'); END;
CREATE TRIGGER review_suppression_key_registry_no_delete
BEFORE DELETE ON review_suppression_key_registry
BEGIN SELECT RAISE(ABORT, 'suppression key pinned'); END;
