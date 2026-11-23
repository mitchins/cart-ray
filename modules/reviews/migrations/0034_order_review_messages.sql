-- One optional follow-up per order. Frozen content survives retries; an
-- attempted message is never automatically retried after an unknown outcome.
CREATE TABLE order_review_messages (
 merchant_id TEXT NOT NULL, environment TEXT NOT NULL,
 order_id TEXT NOT NULL REFERENCES orders(order_id),
 recipient_hash TEXT NOT NULL CHECK(length(recipient_hash)=64),
 invitation_token TEXT NOT NULL CHECK(length(invitation_token)=64),
 unsubscribe_token TEXT NOT NULL CHECK(length(unsubscribe_token)=64),
 settings_json TEXT NOT NULL, due_at INTEGER,
 receipt_selection_json TEXT, selection_json TEXT,
 state TEXT NOT NULL CHECK(state IN ('pending','claimed','accepted','uncertain','skipped')),
 attempted_at INTEGER, provider_reference TEXT, recovery_provider_reference TEXT, created_at INTEGER NOT NULL,
 PRIMARY KEY(merchant_id,environment,order_id),
 UNIQUE(merchant_id,environment,unsubscribe_token)
);
CREATE INDEX order_review_messages_due ON order_review_messages(merchant_id,environment,state,due_at);
