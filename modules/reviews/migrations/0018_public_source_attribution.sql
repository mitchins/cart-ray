-- An explicit public label is distinct from the private source identity.
ALTER TABLE review_snapshot_provenance ADD COLUMN public_attribution TEXT;
