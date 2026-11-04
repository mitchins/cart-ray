CREATE TABLE review_snapshots (
    store_key TEXT NOT NULL,
    environment TEXT NOT NULL CHECK(environment IN ('test', 'live')),
    snapshot_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    digest TEXT NOT NULL CHECK(length(digest) = 64),
    PRIMARY KEY(store_key, environment, snapshot_id),
    UNIQUE(store_key, environment, revision)
);
CREATE TABLE review_snapshot_subjects (
    store_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('product','service','store')),
    canonical_path TEXT NOT NULL,
    PRIMARY KEY(store_key, environment, snapshot_id, subject_id)
);
CREATE TABLE review_contributions (
    store_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    contribution_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('review','reply')),
    status TEXT NOT NULL CHECK(status IN ('published','pending','rejected','withdrawn')),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    rating INTEGER CHECK(rating IS NULL OR rating BETWEEN 1 AND 5),
    source_date TEXT NOT NULL,
    sort_day TEXT,
    verification TEXT NOT NULL,
    anchors_json TEXT NOT NULL,
    votes_json TEXT NOT NULL,
    PRIMARY KEY(store_key, environment, snapshot_id, contribution_id)
);
CREATE INDEX review_feed_idx ON review_contributions(store_key, environment, snapshot_id, subject_id, status, parent_id, sort_day, contribution_id);
CREATE TABLE review_publications (
    store_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(store_key, environment)
);
CREATE TABLE review_import_guard (ok INTEGER NOT NULL CHECK(ok = 1));
