-- Only requests carrying an eligible invitation increment this backstop.
CREATE TABLE acquisition_rate_backstop (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  window INTEGER NOT NULL,
  count INTEGER NOT NULL CHECK(count <= 3000),
  PRIMARY KEY(store_key, environment, window)
);
