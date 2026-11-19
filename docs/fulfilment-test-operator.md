# Test-only fulfilment configuration

Fulfilment is optional and remains test-only. The existing EP delivery lane keeps
its approved account and recipient restrictions. A separate merchant can explicitly
select `post_purchase_generic_test: true` in its installation configuration; that
lane requires its own test account, one approved test recipient, private source,
registered resources, and an explicit delivery activation. It uses the existing confirmed-order
dispatcher and its activation watermark. Registration does not activate sending,
change the checkpoint, or deliver an email. Register before the first deployment;
changing a registered package is intentionally rejected in this initial release.

After provisioning and migrating the installation, put a JSON package in an
operator-owned `0600` file. It has `schema: 1`, the installation's `merchant`,
`environment: "test"`, `bucket` (an existing private R2 bucket name, or null),
`plan`, and `resources`. The plan contains `actions` exactly matching the
installed post-purchase action snapshot, `bindings` associating each workflow and
offer with ordered `{id, version}` resource references, and `no_delivery_offers`
for checkout offers which have no delivery. Each resource has `id`, `version`,
`provider`, `config_ref`, `filename`, `mime_type`, `config`, and `expires_at`
(null for no expiry). The catalogue's authorised checkout offer must separately
pin the same `id@version` resource references. An `external_link` config contains
an approved HTTPS `url` and `allowed_hosts`; a `private_object` config contains
`bucket_binding: "FULFILMENT_PRIVATE_BUCKET"` and the private object `key`.
Provider locators are kept in the protected installation vault, never in D1 or
the public catalogue. Generic test actions use an empty `links` list; the email
handler receives purchase-scoped links from the registered resources. Existing EP
test action and recipient restrictions still apply.

Run `python modules/reviews/operator/operate.py --directory <installation> --credentials
<credentials.json> fulfilment-register --package <private-package.json>
--confirm-test-registration`, then build, deploy and verify using the existing
operator commands. The test bucket must already exist in the configured account;
the installer checks its identity and verifies the deployed binding. It does not
create the bucket. For an externally owned storefront, its operator must route
`GET` and `HEAD /fulfilment/access` to the private CartRay service, preserving
the query string and `CF-Connecting-IP`, without logging the capability. This
route is separate from the checkout API prefix. No site routing is changed by
registration. Hosted routing and delivery need their own qualification.

An access URL is a transferable bearer capability; keep it out of logs and
analytics. Private objects are streamed after entitlement validation. External
links redirect to the configured provider, which may have its own independent
access and revocation behaviour; CartRay cannot revoke a provider URL already
copied by a customer. Expiry and operator revocation apply when resolving a
CartRay capability. Links remain usable when new post-purchase sending is paused.

Use `fulfilment-revoke` for an audited individual entitlement. An interrupted
preparation that never began a provider call can be inspected with
`scripts/post_purchase_recovery.py inspect --fulfilment` and, after checking the
evidence, explicitly reconciled with `fulfilment-recover-prepared`. A claim that
may have reached a provider must remain uncertain until an operator establishes
the outcome; do not use recovery to resend it. The automatic dispatcher does not
blindly resend uncertain messages.
