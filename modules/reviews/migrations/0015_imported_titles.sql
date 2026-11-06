-- Optional public title, separated to preserve the v1 contribution storage shape.
CREATE TABLE review_import_titles (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  contribution_id TEXT NOT NULL, title TEXT NOT NULL,
  PRIMARY KEY(store_key, environment, snapshot_id, contribution_id)
);
