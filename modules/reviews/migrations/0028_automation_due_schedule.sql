-- Test-only queue schedule. Enqueuing is a hint and never authorizes delivery.
CREATE TABLE review_automation_due_schedule (
  store_key TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL, outbox_id INTEGER NOT NULL,
  claim_token TEXT NOT NULL CHECK(length(claim_token)=64),
  due_at INTEGER NOT NULL CHECK(due_at>=0),
  PRIMARY KEY(store_key,environment,acquisition_id),
  UNIQUE(store_key,environment,outbox_id),
  FOREIGN KEY(store_key,environment,acquisition_id)
    REFERENCES review_automation_intents(store_key,environment,acquisition_id)
);
