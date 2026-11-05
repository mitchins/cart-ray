-- Customer-facing names are explicitly supplied by an administrator; internal
-- product keys and paths are not suitable substitutes for public labels.
CREATE TABLE review_subject_display_names (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  public_name TEXT NOT NULL CHECK(length(trim(public_name)) BETWEEN 1 AND 200),
  PRIMARY KEY(store_key, environment, subject_id),
  FOREIGN KEY(store_key, environment, subject_id)
    REFERENCES review_subjects(store_key, environment, subject_id)
);
