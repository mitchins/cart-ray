-- Native staff replies are separately revisioned and do not alter review ratings.
CREATE TABLE native_staff_replies (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, review_id TEXT NOT NULL,
  reply_id TEXT NOT NULL, reply_revision_id TEXT NOT NULL, parent_revision_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('published','withdrawn')),
  PRIMARY KEY(store_key, environment, review_id),
  UNIQUE(store_key, environment, reply_id)
);
CREATE TABLE native_staff_reply_revisions (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, reply_id TEXT NOT NULL,
  reply_revision_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY(store_key, environment, reply_revision_id)
);
CREATE TABLE native_staff_reply_requests (
  store_key TEXT NOT NULL, environment TEXT NOT NULL, request_key TEXT NOT NULL,
  actor TEXT NOT NULL, payload_hash TEXT NOT NULL, reply_id TEXT NOT NULL,
  reply_revision_id TEXT NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY(store_key, environment, request_key)
);
CREATE TABLE native_staff_reply_audit (
  event_id TEXT PRIMARY KEY, store_key TEXT NOT NULL, environment TEXT NOT NULL,
  review_id TEXT NOT NULL, reply_id TEXT NOT NULL, reply_revision_id TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, occurred_at INTEGER NOT NULL
);
CREATE INDEX native_staff_reply_audit_review_idx
ON native_staff_reply_audit(store_key, environment, review_id, event_id);
CREATE INDEX native_staff_reply_revisions_reply_idx
ON native_staff_reply_revisions(store_key, environment, reply_id, reply_revision_id);
CREATE TRIGGER native_staff_reply_revisions_no_update BEFORE UPDATE ON native_staff_reply_revisions
BEGIN SELECT RAISE(ABORT, 'immutable staff reply revision'); END;
CREATE TRIGGER native_staff_reply_revisions_no_delete BEFORE DELETE ON native_staff_reply_revisions
BEGIN SELECT RAISE(ABORT, 'immutable staff reply revision'); END;
