# CartRay

CartRay is a small, FOSS commerce kernel for static storefronts using Stripe Checkout.

Milestone 0 deliberately contains no Stripe SDK, Worker deployment, business catalogue, or
fulfilment provider. It proves the security-sensitive boundary first:

```text
untrusted browser product keys
    → validated private catalogue
    → immutable order snapshot
    → trusted checkout specification
    → payment gateway port
```

See [the Milestone 0 contract](docs/milestone-0-contract.md). Run the test suite with:

```sh
uv run pytest
```

The [Milestone 1 contract](docs/milestone-1-contract.md) adds a test-mode-only Stripe adapter,
and the [Milestone 2 contract](docs/milestone-2-contract.md) adds the local Worker/D1 checkout
runtime. Neither deploys a Worker or uses live Stripe credentials.

The in-progress [Milestone 3 contract](docs/milestone-3-contract.md) adds a static Pages test
storefront, an active-only public catalogue endpoint, and exact-origin browser CORS.

The in-progress [Milestone 4a contract](docs/milestone-4-contract.md) adds the test-only signed
Stripe webhook settlement kernel and its D1 `OrderConfirmed` outbox boundary. It deliberately does
not deploy the Worker or configure a Stripe webhook endpoint; those operational steps are M4b.

The [Milestone 4b contract](docs/milestone-4b-contract.md) and its
[recorded acceptance evidence](docs/milestone-4b-acceptance-evidence.md) define the test-only
Cloudflare/Stripe activation proof. It is explicitly not a live-mode or production launch.

The in-progress [Milestone 5 contract](docs/milestone-5-contract.md) defines the minimal,
non-authoritative post-Checkout return view.

The [Milestone 5b sandbox acceptance evidence](docs/milestone-5b-acceptance-evidence.md) records
the paid and native-free browser-return proof. It is explicitly not a live-mode launch.

The [Milestone 6a contract](docs/milestone-6-contract.md) introduces the deterministic CSV
catalogue protocol and keeps future Sheets ingestion outside the commerce runtime. The
[Milestone 6b contract](docs/milestone-6b-contract.md) adds a read-only Stripe test-mode preflight
for an explicit CSV and private fulfilment-expansions input.

The [Milestone 6c sandbox acceptance evidence](docs/milestone-6c-acceptance-evidence.md) records
the successful synthetic CSV preflight against Stripe test-mode objects. It is not a production
catalogue import.

The in-progress [Milestone 7a contract](docs/milestone-7a-presentation-contract.md) defines a
separate CSV sidecar for public descriptions and versioned static product images, leaving checkout
facts in the commerce catalogue.

The in-progress [Milestone 7b contract](docs/milestone-7b-contract.md) implements that sidecar
against synthetic data and static fixture images, without importing a business catalogue or
changing the deployed sandbox.
