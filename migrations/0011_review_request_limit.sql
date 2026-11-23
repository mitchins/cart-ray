-- Keep the displayed disclosure separate from the merchant's grant limit.
-- NULL preserves the old derivation: existing v2 grants allowed two requests.
-- No immutable grant, order, capability or delivery is rewritten.
ALTER TABLE review_invitation_checkout_grants ADD COLUMN request_limit INTEGER CHECK(request_limit IN (1,2));
