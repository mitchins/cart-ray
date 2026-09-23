# M11b signed projection acceptance evidence

On 2026-09-23, the existing CartRay test Worker key identified by
`cartray-test-2026-09-01` was recovered as a **public verification key only**.
The Worker kept its M4b private signing secret and signing key ID unchanged.

An operator-authenticated, temporary test-only route read the configured public
SPKI keyring, validated the canonical Ed25519 SPKI encoding, and used the active
private signer to sign a fixed domain-separated challenge. The recovered public
key verified that challenge before the Worker logged its public record. The
temporary operator token was deleted immediately afterward. The route is removed
from the final Worker artifact.

| Public-key fact | Value |
| --- | --- |
| Environment | `test` |
| Key ID | `cartray-test-2026-09-01` |
| SPKI SHA-256 | `sha256:bd1d50e20912cd859b4cc8034791e0b19e4798398bb91e573a929eb62591662c` |
| Raw key, base64url | `OI6FlBG2BvI_lA5VgK_50CWrB6-iHob3QGSYEh74qpE` |
| Pinned artifact | `config/projection-public-keys.test.json` |

The test Worker used the `real-test-subset` catalogue profile at
`sha256:8d3674ae409653d9194f71fcdeb5f7b24f4d8442aac3fd966d0c2612aeba71ee`.
The temporary recovery deployment version was
`a8ea3ecb-7776-469c-b654-e9f1cbe47a4e`.
The route-free final deployment version is
`f8ceb2d0-353c-404f-a295-3c2c05d1e1d6`; its secret inventory contains only
the four pre-existing M4b bindings, and the removed recovery path returns 404.

The independent M11b acceptance harness created one paid SIL Stripe **test**
Checkout Session through the deployed CartRay Worker, retrieved the Session and
its line items directly from Stripe, and verified the `cr_*` projection using
the pinned raw public key with the separate Node verifier:

| Proof fact | Observed value |
| --- | --- |
| CartRay order | `cr_6cd371603353431294b155f32920bbe3` |
| Stripe Checkout Session | `cs_test_b1sHRmSihPRnEqCnFFkeSDkvPvwdsVkdOZeR3nkQ1kklooqis7CxGitZAt` |
| Product | `EP-SIL-2026` |
| Stripe test Price | `price_1UBhgmBLB2xuBxraTe0ota3h` |
| Promotion codes allowed | Yes |
| Independent Ed25519 verification | Passed |
| Checkout completed or payment taken | No |

The test establishes the CartRay-to-Stripe signed projection and independent
verification boundary. It does not grant a Valet entitlement or prove the later
Portal fulfilment path.

The proof was repeated against the final route-free deployment, loading the
verification key from `config/projection-public-keys.test.json`. It again passed
with order `cr_fbc28cf114ed4268a4c7ad7aac1fb7b3` and Stripe test Session
`cs_test_b10137Xq8AvcdJ60VO0kzSmnY2y4EEQzMZGyIACrX4X5t3gcxQIS5W39Gg`.
That Checkout Session was not completed and no payment was taken.

After final review tightened the harness, it additionally required the supplied
key to equal the exact checked-in test keyring and required Stripe's Session
state to be `open` with `payment_status=unpaid` before reporting success. A fresh
run against the same route-free Worker passed with order
`cr_4f1473632e8b42fe943d3d7d3bbfac12` and Stripe test Session
`cs_test_b1WUwgWBAMO9QUud5EVYpxzkijVyiTWW5dy9K7dg3okrlYlSW0RpijI82I`;
the observed Session state was `open`/`unpaid`.
