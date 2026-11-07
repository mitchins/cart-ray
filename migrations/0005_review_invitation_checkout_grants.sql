-- Optional review-request permission, captured before checkout redirect.
-- A grant alone never authorizes sending: payment, recipient and suppression
-- must be established by the independent Reviews subscriber.
CREATE TABLE review_invitation_checkout_grants (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  wording_version TEXT NOT NULL,
  wording TEXT NOT NULL,
  granted_at INTEGER NOT NULL,
  merchant_default_selected INTEGER NOT NULL CHECK(merchant_default_selected IN (0, 1)),
  selection_basis TEXT NOT NULL CHECK(selection_basis IN ('express_opt_in', 'preselected_acceptance'))
);

CREATE TABLE review_invitation_checkout_choices (
  order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
  selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
  merchant_default_selected INTEGER NOT NULL CHECK(merchant_default_selected IN (0, 1)),
  wording_version TEXT NOT NULL,
  wording TEXT NOT NULL,
  recorded_at INTEGER NOT NULL
);
CREATE TRIGGER review_invitation_checkout_choices_no_update
BEFORE UPDATE ON review_invitation_checkout_choices BEGIN
  SELECT RAISE(ABORT, 'checkout choices are immutable');
END;
CREATE TRIGGER review_invitation_checkout_choices_no_delete
BEFORE DELETE ON review_invitation_checkout_choices BEGIN
  SELECT RAISE(ABORT, 'checkout choices are immutable');
END;

CREATE TRIGGER review_invitation_checkout_grants_no_update
BEFORE UPDATE ON review_invitation_checkout_grants BEGIN
  SELECT RAISE(ABORT, 'checkout grants are immutable');
END;

CREATE TRIGGER review_invitation_checkout_grants_no_delete
BEFORE DELETE ON review_invitation_checkout_grants BEGIN
  SELECT RAISE(ABORT, 'checkout grants are immutable');
END;
