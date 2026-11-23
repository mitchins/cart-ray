# Historical purchaser-context state

The post-checkout verification/OTP Worker and its sender are retired. Checkout records
permission directly against the immutable order. The native Reviews lifecycle still checks
consent withdrawal and merchant-wide suppression.

The migrations in this directory and existing suppression-key registry remain intact.
Do not reset an existing registry or generate replacement keys because state is missing.
The supported installer pins the original key and reports a recovery diagnostic on mismatch.
