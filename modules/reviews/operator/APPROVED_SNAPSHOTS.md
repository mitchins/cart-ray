# Approved historical snapshots in an installed test Reviews database

This is an explicit **publication operation**, separate from native review eligibility,
checkout, invitations and merchant setup. It applies an already privacy/content-approved
public snapshot to the existing installation's Reviews projection. It does not approve
content itself, import private source exports, create purchase eligibility, send email,
change credentials or move an activation watermark/checkpoint. No production operation
is supported. Keep original approved artifacts and their independent review evidence.

The old `modules/reviews/tools/import_snapshot.py` command remains local-test-only.
Do not edit its database name, run hand-written remote SQL, or use a different publisher
DB as a substitute for the installation's actual Reviews D1.

## Approval and exact target

Use the pinned CartRay checkout that contains this operation, the retained installation
directory and the documented private operator credential handoff. No new credentials,
Workers, databases or migrations are needed. Existing operator prerequisites and
permissions apply. The installation must have Reviews installed and all migration
checksums and actual deployed bindings/versions must verify successfully. This operation
uses the existing one-D1 installation; it does not require sending to be enabled.

Copy `approved-snapshot.example.json` **outside Git** and replace every placeholder from
the installation inventory and approved artifact. Its exact fields are required:

- `account_id`, `database_id`, `installation_id`, `store_key`, `environment`: the actual
  installed target, not values inferred from snapshot content. `environment` must be `test`.
- `snapshot_sha256`: SHA-256 of the exact approved snapshot bytes, including whitespace.
- `snapshot_id`, `publication_revision`: the validated snapshot's exact identity/revision.
- `expected_publication`: `null` only for a first publication. For a replacement, use
  `{"snapshot_id":"previous-id","revision":1,"digest":"64-lowercase-hex-digest"}` from
  the prior publication receipt. Do not guess a predecessor or advance a revision to
  force a conflicting import through. A missing receipt/current-state uncertainty needs
  operator investigation before approval, not an empty-database assumption.
- `approval_ref`: a bounded review/approval reference using letters, numbers and
  `:/._#-`. The approver must review both the exact artifact and target/predecessor.

Hash the completed approval file independently and record that hash with its approval.
A file and its matching hash are integrity pins, **not evidence of permission by
themselves**: only the authorised operator may invoke this command after content and
publication approval. The CLI neither obtains nor replaces owner approval.

```sh
shasum -a 256 "$SNAPSHOT" "$APPROVAL"
uv run python modules/reviews/operator/operate.py \
  --directory "$INSTALL" --credentials "$CREDS" \
  apply-review-snapshot --snapshot "$SNAPSHOT" \
  --approval "$APPROVAL" --approval-sha256 "$APPROVAL_SHA256"
```

The command resolves the actual D1 from inventory, verifies account/resource names,
checks the deployed bindings/versions and schema through the existing verifier, validates
the snapshot using the existing public contract, then sends one **explicit REST `batch`**
of generated statements. It accepts no SQL or caller-selected database argument.

## Atomicity, retries and receipt

The batch includes a compare-and-swap predecessor guard; the original import guards
for identity, revision, subject ownership, aliases, linked history and withdrawal
lineage; publication; in-transaction expected count/identity guards; and readback. It
is not split into Wrangler SQL-file upload chunks. A stale predecessor, conflicting
snapshot identity/revision, skipped contribution or late publication failure must
abort the entire batch. Exact replay of the same approved artifact is permitted when
it is still current. A later publication prevents replaying an older approval.

Successful readback produces a protected `0600`, non-overwritten local receipt at
`$INSTALL/review-snapshot-receipts/$APPROVAL_SHA256.json`. It records the exact target,
approval and snapshot digests, predecessor, publication identity, subject/review/reply
counts and timestamp. It contains no review prose, recipient address or capabilities.
Retain it with the approved files. These local receipts are not cryptographic signatures
or immutable cloud audit records. Retry cannot replace a different existing receipt.

If the provider times out or the local process stops after committing, **do not change
the approval or fabricate a new snapshot**. Retry the identical command: the same
artifact/current-publication guard permits reconstruction of the missing receipt.
If bindings drift, run `doctor` and restore/reverify the intended deployment first.
If readback differs, do not publish downstream static artifacts; investigate and retry
only the identical approved artifact when the target has been established.

This operation does not roll back a database or republish an older snapshot. Correct
content through a separately approved forward snapshot that preserves the existing
alias/identity/withdrawal rules. Native reviews, keys, orders, suppression and mail state
are not rewritten. Downstream Astro/static rendering remains separately gated.

## Evidence boundary before first hosted consumption

`installed_snapshot_workerd.py` executes the exact generated batches against an actual
local Workerd/D1 binding, including late-failure rollback, stale/conflicting revisions,
ignored-contribution rollback and replay. Unit tests exercise the installed operator
and the explicit single REST batch wire shape. **Neither is a hosted REST execution.**

Cloudflare documents the explicit `batch` body in its
[REST query contract](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
and rollback for [D1 binding batches](https://developers.cloudflare.com/d1/worker-api/d1-database/).
Before first hosted application, independent review must accept the transport evidence;
any required real REST late-failure qualification needs separately authorised disposable
D1 scope and a retained receipt. No such hosted mutation or publication was performed
by this implementation. Do not present local rollback tests as remote proof.
