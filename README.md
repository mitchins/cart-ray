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
