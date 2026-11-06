-- Calendar-day order is used only for a merged native/historical feed. It does
-- not reinterpret the precision or timezone of any imported source timestamp.
CREATE INDEX native_unified_feed_idx ON native_reviews (
  store_key, environment, subject_id, state,
  strftime('%Y-%m-%d', created_at, 'unixepoch') DESC, review_id
);
