# Milestone 2 contract

## Scope

Milestone 2 adds the test-only `POST /checkout` Worker entry point and its D1 order store. It does
not deploy the Worker, apply a remote migration, configure secrets, receive Stripe webhooks, or
call Valet.

## Environment bindings

The Worker fails closed unless these bindings exist: `CARTRAY_ENVIRONMENT=test`, `STRIPE_API_KEY`,
`CARTRAY_SIGNING_KEY_ID`, `CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64`, `CARTRAY_SUCCESS_URL`, and
`CARTRAY_CANCEL_URL`. The signing key is an Ed25519 PKCS#8 private key encoded with standard base64.
`STRIPE_API_KEY` and the private key are Worker secrets, never Wrangler vars or repository files.

## Local runtime

Pyodide's current launcher requires Node 24.20.0. The repository records that version in `.nvmrc`;
run Worker commands through `nvm exec 24.20.0 uv run pywrangler …`. This does not change the system
Node version. `npm ci` installs the pinned Wrangler version used by Pywrangler; CI selects the same
Node and runs both the Worker health check and a Workerd D1/Web Crypto smoke test.

## Activation gate

Before public test deployment, explicitly configure access/CORS and abuse controls; provision the
test-only secrets; apply `0001` remotely; deploy; then exercise a real Stripe Checkout Session and
signed webhook acceptance flow. A fresh checkout returns `201`; a persisted idempotent replay will
return `201` in this milestone until response provenance is exposed in the store.
