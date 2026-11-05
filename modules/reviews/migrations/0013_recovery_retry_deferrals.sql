-- Defer a temporarily unavailable recipient without losing its request.
CREATE TABLE review_recovery_deferrals (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  acquisition_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id)=64),
  retry_after INTEGER NOT NULL,
  PRIMARY KEY(store_key, environment, acquisition_id)
);
