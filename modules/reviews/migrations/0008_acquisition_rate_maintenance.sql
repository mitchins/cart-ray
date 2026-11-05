CREATE TABLE acquisition_rate_maintenance (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  operations INTEGER NOT NULL,
  PRIMARY KEY(store_key, environment)
);
