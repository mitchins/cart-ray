# R5 publication experiment — test only

This independent Worker consumes the public R4 Reviews projection. It has its
own D1 database and does not write to CartRay checkout or Reviews storage. It is
not enabled by the Reviews module. All endpoints require both
`PUBLICATION_MODE=test` and `PUBLICATION_ENVIRONMENT=test`; write endpoints also
require a fresh test token. There is no production configuration or approved
production activation in this experiment.

## Publication contract

A publisher supplies a complete R4 subject lookup, summary and all contribution
pages, pinned to one source snapshot. The normalizer rejects inconsistent
versions, incomplete pages, duplicate identities, bad aggregates and oversized
sets. It retains public dates and their provenance, review identity lineage,
replies, verification claims, votes and anchors. The digest covers the canonical
normalized projection, canonical storefront origin, source Worker origin,
source scope, exact rendered HTML and JSON-LD bytes, and media bytes and ownership. An independently supplied digest
must match before promotion. The Worker re-fetches the R4 lookup, summary and
pages and compares the normalized projection before activation and on every
serve. The publisher-supplied subject name and origin still require independent
approval; the public R4 lookup does not attest the store/environment identity.

HTML and product-only JSON-LD are rendered from the same object, and responses
carry the same publication digest. Consumers that request them separately must
check the digests match and discard both on mismatch. All public responses and
media carry browser and Cloudflare no-store headers. Media is restricted to
pinned, size-bounded PNG/JPEG/WebP data and is only served through this gate.
Third-party widgets, external videos and assets copied outside the gate are not
covered. No media rights or production migration approval is implied.

## Synthetic deployment and emergency withdrawal

1. Create a unique disposable Worker and D1 database; apply `schema.sql`. Bind
   only synthetic test data and an independent R4 test Worker. Configure the
   source origin, explicit `example-store:test` scope, canonical test origin,
   test mode and a fresh secret token. Do not use production bindings.
2. Capture the full R4 projection, independently calculate and approve its digest,
   then use the test-only publication endpoint. Never expose the test token in a
   storefront. Record source revision, artifact digest and deployment identity.
3. Verify HTML, JSON-LD and every gated media URL share the approved digest and
   are no-store; exercise the safe fallback before making any route reachable.
4. For an emergency, submit the specific record ID, current digest and gate epoch to the
   test-only withdrawal endpoint. The D1 transaction switches the whole publication
   to the safe response and retains record and historically associated media
   tombstones. A version mismatch or unavailable R4 source also quarantines the
   current digest. Verify HTML, JSON-LD and media return the safe response, then
   rebuild and independently approve a new projection that excludes tombstones.
   If quarantine was caused only by a transient source outage, a test-only reviewed
   recovery can reactivate the same digest after a fresh full-source comparison,
   an epoch check and verification that no record or media tombstones apply.
5. To roll back a deployment, first put the gate in safe mode. Keep the D1 gate,
   retired digests and tombstones intact. Never restore an old artifact or route
   around the gate; verify fallback and cache behavior before explicitly approving
   a replacement. An old HTML/JSON-LD build containing withdrawn content is not a
   safe rollback target. Delete disposable test resources after qualification.

## Limits and release gates

This is a synthetic, live-validation prototype. Each served response reads the
publication gate from the primary database and compares a complete current R4
projection, which is deliberately conservative and not production-scale
performance qualification. The 30-second client lease is a best-effort refresh,
not a hard 60-second guarantee when JavaScript, browser timers or an entire
browser is suspended. A response already in flight when withdrawal commits can
finish afterward. No-store prevents this Worker from creating caches; it cannot
purge a cache or copied file outside its control. Real static-site routing, CDN
rules, cache and media inventory, cross-account trust, operator identity,
retention, latency/load, and safe recovery from database or source failures all
require hosted qualification and owner-approved operations before production.
A production-capable source needs a trustworthy store/environment identity and a
monotonic publication/revocation protocol; the current public R4 shape does not
attest those values. Source outage quarantine is intentionally conservative.
