# Order confirmations and Reviews

New test merchants use the schema-2 order-confirmation configuration introduced
in #94. Configure one transactional provider (`smtp2go` or `cloudflare`), its
merchant sender, contact details and approved sandbox recipient. Reviews uses
that same provider for confirmation inclusion, an optional follow-up and
customer-requested replacement links.

Optional non-secret installation preferences:

```json
{
  "reviews_enabled": true,
  "review_requests": {
    "timing": "receipt",
    "selection": "priority",
    "delay_days": 7,
    "priorities": ["PRODUCT-ONE", "PRODUCT-TWO"]
  }
}
```

`timing`: `off`, `receipt` (default), `followup`, or `both`.
`selection`: `priority` (default) or `all`. Priorities are merchant offer keys;
subjects are deduplicated and unmapped priorities fall back to subject identity.
Delay is in days (0–365); fractional days are useful for sandbox qualification.
Selection affects the featured products, never the purchase entitlement. The
invitation page retains every eligible product and its submission progress.

The `both` preference uses a versioned checkout disclosure covering receipt
inclusion and one follow-up. Old clients and existing one-request grants remain
limited to one solicitation; changing site preferences does not expand them.

The installer provisions Commerce, native Reviews and the private shared email
handler, sharing the merchant's existing one D1. No intake producer, purchaser
attestor, Reviews sender or automation queue is provisioned for schema-2 stores.
Commerce's minute trigger handles bounded order mail/recovery work; its separate
hourly trigger retains payment reconciliation. The complete notification drain
has a 22-second budget. Claims are committed before provider handoff. Unknown
outcomes remain held and are never automatically resent.

Use the existing supported installer sequence:

```
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS provision
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS migrate
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS build
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS deploy
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS activate --enable-test-delivery
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS doctor
uv run python modules/reviews/operator/operate.py --directory PRIVATE_INSTALL --credentials PRIVATE_CREDENTIALS pause
```

If a merchant input package fails full review-page validation before its first
successful build, correct the source package and repeat `build --merchant-inputs
PATH`. This explicitly snapshots and validates the correction while sending is
OFF, preserving provisioned resources, keys and checkpoints. It does not weaken
catalogue/Price-lock compatibility or overwrite a retained build artifact.

Activation explicitly initializes the existing order checkpoint at the current
outbox maximum, excluding historical purchases. Repeated activation preserves
it. Pause disables both sides of the shared mail path; checkout, order access,
review management and unsubscribe remain available. No public email endpoint
or browser signing secret is introduced.

Capabilities are generated unpredictably and committed with all eligible
subjects before the first provider send. Checkout permission, its disclosure
and default-state evidence, the pinned suppression key, consent withdrawals and
merchant-wide suppression govern solicitation. They do not suppress the paid
receipt. Provider or Reviews failure cannot authorize another receipt attempt.

A follow-up uses the same invitation and the current unreviewed product state.
Its selection is frozen atomically with its sole send claim. A replacement uses
the existing native generation protocol; native activation, recovery completion
and the order's current token copy are updated atomically. Existing native
capabilities are never overwritten or widened by the new processor.

Inspect ambiguous receipt claims using `scripts/post_purchase_recovery.py` as
before. Add `--include-review-followups` when inspecting a shared Reviews
database. It shows purpose, attempt time and provider reference without
capabilities or recipient details. An interrupted follow-up cannot block later
orders; it remains held for provider-evidence investigation. This command does
not resend mail.

Retained queue-era installations are recognized from their resource inventory.
Their old deployed artifacts, keys, uncertain claims and checkpoints are not
rewritten by this change. They require a deliberate, paused handover; this PR
does not perform that operation on the retained EP UAT installation. Native
redemption and original recovery routes remain supported. New order recovery
only consumes acquisitions owned by the order notification ledger.

Application rollback is compatible only with the retained schema and keys and
an OFF mail deployment. Never restore an old database as ordinary rollback or
run old workflow senders alongside the order-confirmation processor.

Local evidence: the connected tests run a two-product purchase, native rating
submission before email acceptance, a follow-up selecting the outstanding
product, replacement recovery, opt-out, replay and ambiguous acceptance. The
Workerd/D1 smoke runs the purchase-to-native-review-to-follow-up path and
suppression against real local D1 SQL. Synthetic provider tests cover SMTP2GO
and Cloudflare request formats; they are not live provider qualification.
