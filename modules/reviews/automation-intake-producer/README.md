# Retained subscriber migration history

The intake producer runtime, management-API verifier and identity routes are
retired by PRODUCT-SHAPE-01. This directory retains only its additive historical
SQL migrations, applied by the operator to preserve migration ordering and
existing state. It is not a Worker deployment or an activation procedure.

New stores use the existing confirmed-order checkpoint, one confirmation
processor and one shared private email handler. See `docs/order-reviews.md`.
Retained queue-era installations must use their pinned release until a separately
authorized paused handover; never reset their claims or replay historical mail.
