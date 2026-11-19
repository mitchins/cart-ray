# Public GET qualification (development, not an activation approval)

This record concerns isolated local Workerd runs on the PR #86 development branch.
No production traffic or Stripe credentials were used. Wrangler 4.127.1 used local D1,
the `real-test-subset` locked build profile and an explicit local rate-limit binding.
The disposable test database contained one synthetic pending checkout.

| Path | Local observation | Backend cost / qualification |
| --- | --- | --- |
| `/catalogue`, with and without Origin | 200 in Worker tests; 200 in local Workerd without Stripe credentials | Compiled public snapshot; no Stripe or D1 on the read. A locked build is required. |
| `/health` | 200 | No Stripe or D1. Liveness only; it is not a database readiness probe. |
| `/checkout-status`, malformed ID | 404 | Rejected before D1. |
| `/checkout-status`, unknown valid ID | 404 | At most one indexed D1 read after IP budget admission. |
| `/checkout-status`, 65 requests from one address | 60 × 404, 5 × 429 | Local Workerd, no Origin. 429 follows rate-limit rejection before D1. |
| `/checkout-status`, synthetic pending ID | 200, `{"state":"pending"}` | Local Workerd/D1, no Origin, `Cache-Control: no-store`. |
| EP Classic connected product page, idle | Zero `/catalogue` or `/__uat/context` browser requests | Local Chromium synthetic composed PR #83/#85 build; one request to each after Add. |
| Generic connected GEL product | Catalogue fetch on page load | Compiled snapshot avoids Stripe/D1 but still invokes commerce Worker. |
| Public Reviews summary GET | Local Workerd/D1: 120 × 200, then 429; a second client receives 200 | Separate edge-admission budget. Hosted client-IP propagation and withdrawal/cache behavior still require qualification. |
| Public Reviews contributions and subject lookup | Python tests reject denied requests before D1 | Response and cursor bounds are preserved; hosted behavior still requires qualification. |
| Static files, HEAD, query variants and unknown paths | Not yet fully qualified against deployed routing | Hosted and cross-renderer route matrix remains required. |

The local runtime initially failed to import `kinglet` because pywrangler's sync left its
vendored wheel absent. The existing locked-vendor repair was used before starting the local
Worker; no dependency versions were changed. This result is not evidence about hosted client-IP
propagation or Cloudflare's deployed rate-limit distribution. Do not enable the public UAT API
until the outstanding routing, Reviews and hosted checks are complete.

## Integrated checkout and receipt (local Workerd)

The integrated Worker built from `056a37b` was run locally with the locked
`real-test-subset` profile and a disposable migrated D1; no Stripe credentials
were supplied. `/catalogue` returned 200. A syntactically invalid status ID
returned 404. An unframed checkout upload returned 400; a framed request reached
the configured-service boundary and returned 503 because Stripe credentials
were deliberately absent. One client's 21 checkout requests returned 20 × 503
then 429, while another client still reached the service boundary (503). This
is local rate-limit evidence, not evidence of a successful hosted checkout.

A synthetic confirmed two-unit order returned the following allowlisted result;
private Stripe-price and fulfilment canaries in D1 were absent:

```json
{"state":"confirmed","receipt":{"currency":"aud","items":[{"product_key":"TEST-PRODUCT","quantity":2,"unit_amount_minor":50}]}}
```

A separate instrumented test of the real catalogue builder and Stripe gateway
measured four Price GETs plus two Session POSTs for a four-source new checkout,
plus one PaymentIntent POST when supplied; an attached retry made four Price
GETs and no POSTs. The test did not call the network. Rate-limiter counters
are approximate and location-scoped, not a globally strict quota.
