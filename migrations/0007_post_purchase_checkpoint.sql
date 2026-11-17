CREATE TABLE post_purchase_checkpoint (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  merchant_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  activation_watermark INTEGER NOT NULL CHECK(activation_watermark >= 0),
  last_outbox_id INTEGER NOT NULL CHECK(last_outbox_id >= activation_watermark)
);
