-- An operator selects one held state and pages its identities without scanning all deliveries.
CREATE INDEX invitation_staff_queue_idx
ON invitation_generations(store_key, environment, state, delivery_identity);
