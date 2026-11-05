CREATE TABLE native_management_requests (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  request_key TEXT NOT NULL,
  review_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('edit','withdraw')),
  resulting_revision TEXT NOT NULL,
  resulting_state TEXT NOT NULL,
  PRIMARY KEY(store_key, environment, request_key)
);
