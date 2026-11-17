# Effective Policy catalogue UAT

This is a local, no-index UAT audit of the approved public/demo handoff, not a production shop, delivery system or WordPress migration. It displays all published entries, including source-hidden entries marked as such, for audit; it must not be published as the merchant's production catalogue. It uses existing GEL assets and gallery code, but the UAT page builder is not yet a supported generic merchant storefront or installer.

The source archive is intentionally not in this repository. Prepare a snapshot with `uv run --locked python examples/ep-uat/prepare.py --help` and build it with:

```sh
uv run --locked python examples/ep-uat/build.py /path/to/prepared /path/to/new-output --theme minimal
```

Select `minimal`, `studio` or `classic`. The default EP UAT GEL is `gel.json`; supply a merchant-specific validated manifest with `--gel`. Its identity and currency must match the catalogue, and only the approved source logo may be used. Colours are provisional UAT choices. The builder validates source image fingerprints and copies only declared approved media. The local routes are UAT routes, not source canonical URLs. Browse-only products remain visible without payment or entitlement mappings. Variations are selectable under their source parent; they are not checkout-authorised.

## Controlled sandbox lane

`provision_test_prices.py --help` explains the opt-in sandbox fixture: the approved source archive is checked again, selected source prices are explicitly mapped to Stripe test Prices, and dummy fulfilment is separate from source catalogue authority. Keep the generated fixture and test key private. Never use a live key, real paid files, or real customer data. No follow-up email is sent; the supplied test-only Dropbox follow-up can be displayed only after the appropriate test grant.

Build with `--test-fixture /private/fixture/test-price-map.json`, then run `test_server.py --help`. The server binds loopback only, uses real Stripe test mode and CartRay's existing checkout/settlement code, and grants a harmless dummy file to the same browser context. It is a local qualification harness backed by SQLite, not a production download service, a supported merchant deployment, or evidence of cross-device delivery. Checkout remains disabled when the test server is absent. No production consent or Reviews automation is enabled.

To run browsing checks, install the matching Playwright browser versions and use `browser_qualification.py --help`. Scaling snapshots made by `scale_fixture.py` are explicitly synthetic and are not source data.
