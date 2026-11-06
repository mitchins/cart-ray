-- Explicitly adjudicated same-review identities; no author/content heuristics.
CREATE TABLE review_identity_links (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('test','live')),
  subject_id TEXT NOT NULL,
  source_identity TEXT NOT NULL,
  source_contribution_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL,
  source_snapshot_sha256 TEXT NOT NULL CHECK(length(source_snapshot_sha256)=64),
  native_review_id TEXT NOT NULL,
  native_revision_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  PRIMARY KEY(store_key,environment,source_identity,source_contribution_id),
  UNIQUE(store_key,environment,native_review_id)
);
CREATE TRIGGER review_identity_links_validate BEFORE INSERT ON review_identity_links
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM native_reviews n JOIN native_revisions v
      ON v.store_key=n.store_key AND v.environment=n.environment
      AND v.review_id=n.review_id AND v.revision_id=n.revision_id
    JOIN native_publication_versions pv
      ON pv.store_key=n.store_key AND pv.environment=n.environment AND pv.subject_id=n.subject_id
    WHERE n.store_key=NEW.store_key AND n.environment=NEW.environment
      AND n.subject_id=NEW.subject_id AND n.review_id=NEW.native_review_id
      AND n.revision_id=NEW.native_revision_id AND n.state='published'
  ) OR NOT EXISTS (
    SELECT 1 FROM review_publications p
    JOIN review_snapshots snapshot ON snapshot.store_key=p.store_key AND snapshot.environment=p.environment
      AND snapshot.snapshot_id=p.snapshot_id AND snapshot.digest=NEW.source_snapshot_sha256
    JOIN review_snapshot_provenance s ON s.store_key=p.store_key AND s.environment=p.environment
      AND s.snapshot_id=p.snapshot_id AND s.source_identity=NEW.source_identity
    JOIN review_contributions c ON c.store_key=p.store_key AND c.environment=p.environment
      AND c.snapshot_id=p.snapshot_id AND c.subject_id=NEW.subject_id
      AND c.contribution_id=NEW.source_contribution_id AND c.revision_id=NEW.source_revision_id
      AND c.parent_id IS NULL AND c.status='published'
    WHERE p.store_key=NEW.store_key AND p.environment=NEW.environment
  ) THEN RAISE(ABORT,'invalid identity link') END;
END;
CREATE TRIGGER review_identity_links_insert AFTER INSERT ON review_identity_links BEGIN
  UPDATE native_publication_versions SET version=version+1
  WHERE store_key=NEW.store_key AND environment=NEW.environment AND subject_id=NEW.subject_id;
END;
CREATE TRIGGER review_identity_links_no_delete BEFORE DELETE ON review_identity_links BEGIN
  SELECT RAISE(ABORT,'identity links are immutable');
END;
CREATE TRIGGER review_identity_links_no_update BEFORE UPDATE ON review_identity_links BEGIN
  SELECT RAISE(ABORT,'identity links are immutable');
END;
