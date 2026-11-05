-- A recovery request never records an address or an invitation bearer token.
-- One row per acquisition bounds storage and repeated requests.
CREATE TABLE review_recovery_requests (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id)=64),
  requested_at INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','fulfilled','failed')),
  PRIMARY KEY(store_key, environment, acquisition_id),
  FOREIGN KEY(store_key, environment, acquisition_id)
    REFERENCES acquisition_invitations(store_key, environment, acquisition_id)
);
CREATE INDEX review_recovery_pending_idx ON review_recovery_requests(store_key,environment,state,requested_at);
