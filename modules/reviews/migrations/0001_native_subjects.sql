-- Applied only by an explicitly installed Reviews deployment, never CartRay core.
CREATE TABLE IF NOT EXISTS review_subjects (
    store_key TEXT NOT NULL CHECK (length(store_key) BETWEEN 1 AND 128),
    environment TEXT NOT NULL CHECK (environment IN ('test', 'live')),
    subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 128),
    kind TEXT NOT NULL CHECK (kind IN ('product', 'service', 'store')),
    canonical_path TEXT NOT NULL CHECK (length(canonical_path) BETWEEN 1 AND 2048),
    PRIMARY KEY (store_key, environment, subject_id)
);
