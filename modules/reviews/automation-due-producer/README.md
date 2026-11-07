# Test-only addressed due scheduler

This adapter accepts authenticated internal `POST /schedule` with exactly an
`outbox_id`. It selects only an existing accepted intent for the configured
store and `test` environment; pre-activation, late, unknown and other-store
orders cannot create a schedule. It persists one random claim token and the
original due time before enqueueing a minimal hint. Retries reuse that token.

`SCHEDULE_QUEUE` redelivers the hint in hops no longer than 43,200 seconds.
At the persisted due time it enqueues the existing due-evaluation command on
`DUE_QUEUE`. Duplicate messages are possible after crashes or uncertain queue
acceptance: the existing evaluation claim deduplicates them by the persisted
acquisition and token. Failures retry through the queue's bounded retries and
DLQ. Re-invoking `/schedule` after a failed enqueue is safe; the caller must
retry failed scheduling and retain its upstream message until acknowledged.
There is no database scan or polling loop.

This is a disabled-by-default, test-only producer. The authenticated intake consumer can invoke it after recording a grant when
`AUTOMATION_DUE_SCHEDULING_ENABLED=test` and a private `DUE_SCHEDULER` binding
are configured. Set the consumer `DUE_SCHEDULER_TOKEN` to match the scheduler
`REVIEWS_DUE_SCHEDULER_TOKEN`, distinct from the other tokens. Bind test Reviews
D1 and two isolated queues. This change does not deploy resources. The existing due
consumer only marks an evaluation as held: scheduling is not permission to
send or create an invitation. Recipient and consent verification, initial
invitation issuance and delivery remain separate work.
