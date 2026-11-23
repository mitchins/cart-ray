-- Provider identities are immutable; existing SMTP2GO receipts remain untouched.
CREATE TABLE review_cloudflare_deliveries (
  store_key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment='test'),
  acquisition_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  delivery_identity TEXT NOT NULL,
  provider_message_id TEXT NOT NULL CHECK(length(provider_message_id) BETWEEN 1 AND 512),
  accepted_at INTEGER NOT NULL CHECK(accepted_at>0),
  PRIMARY KEY(store_key,environment,acquisition_id,generation_id),
  UNIQUE(store_key,environment,provider_message_id),
  UNIQUE(store_key,environment,delivery_identity),
  FOREIGN KEY(store_key,environment,acquisition_id,generation_id)
    REFERENCES invitation_generations(store_key,environment,acquisition_id,generation_id)
);
CREATE TRIGGER cloudflare_delivery_no_update BEFORE UPDATE ON review_cloudflare_deliveries
BEGIN SELECT RAISE(ABORT, 'provider delivery immutable'); END;
CREATE TRIGGER cloudflare_delivery_no_delete BEFORE DELETE ON review_cloudflare_deliveries
BEGIN SELECT RAISE(ABORT, 'provider delivery immutable'); END;
