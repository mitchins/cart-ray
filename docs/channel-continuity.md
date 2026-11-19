# Channel feed rehearsal

This offline tool stages deterministic Google TSV and Meta CSV files from explicit merchant-owned snapshots. It does not contact a channel, publish a feed, change ownership, or confer checkout authority. The samples are synthetic. Destination-specific acceptance, account permissions and eligibility remain rollout gates.

The snapshot records merchant, channel, account, catalogue, content language, feed label, source, capture time and explicit complete coverage. Every item retains its external ID, independent merchant product or variation ID, exact landing and image URLs, SEO canonical URL, price, availability, grouping and explicit publication/eligibility. Channel item IDs must come from the existing channel inventory, not be generated from a CartRay key. A listing does not authorize checkout or fulfilment.

```sh
uv run --locked python -m cartray.channels validate examples/channel-continuity/squirrel-demo-google.json
uv run --locked python -m cartray.channels export examples/channel-continuity/squirrel-demo-google.json /tmp/squirrel-feed.tsv
uv run --locked python -m cartray.channels compare source.json candidate.json
```

Export creates a new file, refuses to overwrite an existing file and refuses a header-only feed. Compare exits nonzero for an unapproved difference. A reviewed exception is a JSON list of objects containing the source and candidate SHA-256 digests printed by compare, item ID, field, reviewer and reason. Exceptions apply only to those exact snapshots. Obtain snapshots from the actual channel and verify coverage; a self-declared sample is not evidence of current production state.

A feed file is a staged artifact, not evidence of ingestion or permission to switch sources. Before a separately authorized transition, record the previous source and feed URL and verify item-level ingestion and acceptance in an isolated account. Same-ID uploads in a live account can overwrite existing items. Do not run two competing writers. Roll back by restoring the prior source configuration and verifying processed item statuses; do not rewind commerce data. Account ingestion and rollback remain unqualified until an actual sandbox channel account is available.

A successful comparison and `locally_exportable` only mean the configured local checks passed; they do not establish channel acceptance, account-specific brand/identifier, shipping, tax or destination requirements. Exported cells preserve merchant-supplied text and IDs exactly: treat feed files as untrusted data when opening them in spreadsheet software. Exceptions bind the complete snapshot digest, so even a reordered snapshot requires a fresh review. Multiple channel item IDs may intentionally map to one merchant identity; changes are reported as separate additions and removals.

Google quoted TSV values are supported by [Google's quoted-attribute format](https://support.google.com/merchants/answer/14998273). Google IDs and group IDs are constrained by its [50-character limits](https://support.google.com/merchants/answer/6324405); unsupported channel-specific fields and unqualified availability modes are held rather than silently removed. Zero-price subscription exceptions, `variant_option` group attributes, and live account ingestion are not supported by this first adapter.
