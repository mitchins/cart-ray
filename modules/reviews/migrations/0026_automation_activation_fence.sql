-- A test-only outbox snapshot marks the linearization point for new activation.
-- Legacy activation records retain their original timestamp and watermark rules.
CREATE TABLE review_automation_fence (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  fence_mode TEXT NOT NULL CHECK(fence_mode='outbox_snapshot'),
  PRIMARY KEY(store_key, environment),
  FOREIGN KEY(store_key, environment) REFERENCES review_automation_activation(store_key, environment)
);
CREATE TRIGGER review_automation_fence_immutable_update BEFORE UPDATE ON review_automation_fence
BEGIN SELECT RAISE(ABORT, 'immutable activation fence'); END;
CREATE TRIGGER review_automation_fence_immutable_delete BEFORE DELETE ON review_automation_fence
BEGIN SELECT RAISE(ABORT, 'immutable activation fence'); END;
CREATE TRIGGER review_automation_fence_immutable_replace BEFORE INSERT ON review_automation_fence
WHEN EXISTS (SELECT 1 FROM review_automation_fence f
             WHERE f.store_key=NEW.store_key AND f.environment=NEW.environment)
BEGIN SELECT RAISE(ABORT, 'immutable activation fence'); END;
