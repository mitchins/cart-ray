# Test-only recovery consumer

This optional Worker consumes recovery commands from a private Cloudflare queue. Configure `REVIEWS_RECOVERY_AUTOMATIC=1` and a `REVIEWS_RECOVERY_QUEUE` producer binding on the Reviews Worker; automatic mode fails closed if that binding is missing. Configure this consumer with a service binding named `REVIEWS_API` to the Reviews Worker, the independently provisioned issuer and delivery secrets, and a `MAIL` email binding restricted to the approved synthetic test mailbox. Its `REVIEWS_ENVIRONMENT` must be `test`.

The test deployment must set `SYNTHETIC_RECIPIENT`, `SYNTHETIC_SENDER`, `REVIEWS_PUBLIC_STORE_NAME`, and `REVIEWS_LANDING_URL`. The recipient is accepted only when its hash matches the independently consent-bound acquisition. The sender and destination must be restricted in the test email binding. No arbitrary address from an invitation or queue command is accepted.

Configure a dead-letter queue and retain unresolved messages for operator action. A command older than 24 hours can continue an existing pending request but cannot create or reopen one; a stale unprocessed command is retried and must reach the dead-letter queue. Once delivery has been claimed, a lost response or ambiguous send is never retried as a send. Provider reconciliation requires authoritative evidence of the original acceptance time and delivery identity; otherwise leave it unresolved.

A disabled or non-test Reviews Worker cannot collect or send. This adapter does not enable production collection, campaigns, or scheduled invitations. A real recipient adapter, staff identity, sender policy and operational qualification remain separate release requirements.
