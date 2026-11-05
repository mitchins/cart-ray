# Stalled-upload transport reproducer

Run `modules/reviews/tests/stream_repro/run-linux.sh` from this checkout. It uses Docker, Node 24.20.0 and this repository's npm lockfile (Wrangler 4.127.1 and Workerd 2026-08-28). It runs otherwise equivalent minimal JavaScript and Python handlers, each through a direct Workerd listener and Wrangler's local proxy. No D1 or Reviews business logic is involved.

The probe reports the completed malformed request, a health request during an incomplete upload, the stalled socket's outcome, and ten subsequent health probes. Handler logs distinguish application return from on-wire delivery. A timeout, reset or listener failure is a transport outcome, not an application 400. This reproducer is diagnostic: it reports behavior rather than hiding a proxy failure. It does not qualify the deployed Cloudflare runtime.
