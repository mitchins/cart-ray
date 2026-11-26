# Disposable remote REST-batch qualification

`installed_snapshot_remote.py` prepares the remaining transport evidence for issue #105.
It is not part of merchant installation or the per-application runtime. **Do not run it
until the owner and independent reviewer approve creation/use/deletion of one empty,
unbound disposable D1.** The EP operator owns that resource lifecycle. This tool does
not create, delete, bind or discover databases and never accesses EP content.

On the separately approved target, the operator supplies the exact account, D1 UUID
and a new name starting `cartray-snapshot-proof-`. Before any migration, the runner
verifies actual UUID/name and refuses a database containing any non-internal schema object (including views).
It cannot resume against a nonempty database: retain the receipt and inspect/delete
the separately approved disposable resource rather than bypassing that safeguard.

Use the normal documented `uv`/Node prerequisites. Place only
`{"cloudflare_api_token":"OPERATOR_TOKEN"}` in a private, operator-owned `0600` JSON
file. The token needs access to metadata and D1 writes on this separately approved
target; it is never printed or stored in the receipt. After approval:

```sh
uv run python modules/reviews/tests/installed_snapshot_remote.py \
  --account "$ACCOUNT" --database "$DISPOSABLE_D1_UUID" \
  --database-name "$DISPOSABLE_D1_NAME" --credentials "$PRIVATE_D1_CREDENTIALS" \
  --receipt "$NEW_RECEIPT_JSON" --ack-disposable
```

The fixed synthetic fixture, existing migrations, existing importer compiler,
`installed_snapshot.application_batch()` and production `cloud.API.batch()` provide
one explicit REST batch per operation. Tests cover success/replay, stale/conflicting
identity/revision, predecessor mismatch, a silent skipped contribution caught by the
atomic count guard, and a late publication-trigger abort with unchanged table
fingerprints followed by successful retry. No caller SQL or snapshot is accepted.

Only the specific expected SQLite guard constraint for a guard test, or the exact
late-publication trigger marker for that test, can satisfy a negative test. Transport failure, generic error 7500, authentication failure
or unclassified failure is **inconclusive**, never rollback evidence.

A protected, non-overwritten receipt records PASS/HOLD, exact target, immutable source
fixture hash and derived snapshot identities, exact serialized batch payload hashes,
statement counts, per-failure before/after state fingerprints and final readback.
It contains no content, provider raw errors, tokens or capabilities. A failure may leave
synthetic state/triggers on the disposable resource; no automatic cleanup or retry of
this whole runner is performed. EP must retain evidence and carry out its separately
approved deletion. This runner has not itself been executed remotely by its author.
