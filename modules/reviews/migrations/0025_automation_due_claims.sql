-- Test-only evaluation claims. These never authorize invitation creation or delivery.
CREATE TABLE review_automation_due_claims (
  store_key TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL, claim_token TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation > 0), lease_until INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('claimed','completed')),
  PRIMARY KEY(store_key,environment,acquisition_id),
  FOREIGN KEY(store_key,environment,acquisition_id)
    REFERENCES review_automation_intents(store_key,environment,acquisition_id)
);
