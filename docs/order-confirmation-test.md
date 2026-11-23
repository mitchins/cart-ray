# Test order confirmations

This development slice sends one confirmation for a confirmed order through the
existing private test email handler. It is disabled until the existing
post-purchase test activation is explicitly performed. It is not a production
mail configuration and does not yet include review invitations.

For a new test merchant, the existing `post_purchase_config` input can contain
the following protected JSON, supplied outside the worktree. The addresses must
match the approved test sender and recipient in the operator credential handoff.

```json
{"schema": 2, "sender": "sender@example.test", "recipient": "recipient@example.test"}
```

Use the existing private-source installation, Stripe test account and explicit
no-backfill activation. The existing provider selector chooses SMTP2GO or
Cloudflare; it does not create a second provider for the confirmation. The
fulfilment configuration described in `order-access-test.md` supplies the
accountless order link. If access cannot be issued, the confirmation says that
resources are pending; it is not automatically resent.

## Handover from legacy workflows

This is a deployment transition, not permission to change the existing EP test
installation. Pause the old sender and wait for in-flight requests to finish.
Apply the additive migration before starting the new sender. Existing workflow
claims remain in place: unresolved claims hold the checkpoint and need the
existing operator reconciliation; prior claims do not trigger another email.
The migration prevents a new receipt claim and a legacy workflow claim from
being inserted for the same order, including during a rollback. It does not
cancel a provider request that was already in flight before migration.

Review invitation composition, unified merchant activation, real provider
delivery, and hosted purchase qualification are not established by this slice.
