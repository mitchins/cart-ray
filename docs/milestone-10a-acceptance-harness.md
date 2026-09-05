# Milestone 10a test acceptance harness

`scripts/m10a_acceptance.py` reduces the deployed M10a reconciliation proof to one deliberate
hosted Stripe Checkout click, one real Cron run, and a D1-token-console command. It is strictly
test mode: it accepts only `rk_test_` or `sk_test_` credentials, requires a test Event Destination,
and never stores a credential or raw Stripe payload.

The harness requires a Stripe restricted key that can manage the test Event Destination as well as
retrieve/expire Checkout Sessions. It does not require a Cloudflare token except for the printed
D1 commands, which intentionally run in the separate scoped-token console.

## Start a run

Run this from the normal Stripe-key terminal:

```sh
uv run python scripts/m10a_acceptance.py prepare \
  --destination-id we_1UB69CBLB2xuBxrayoxwttci
```

`prepare` first disables that test destination, then creates two fresh `TEST-FREE` CartRay
Checkouts and writes a mode-`0600` local state file at `/tmp/cartray-m10a-acceptance.json`. Its
JSON output contains the free confirmation Checkout URL and the exact next D1 command. If prepare
fails after disabling the destination, restore it without needing a state file:

```sh
uv run python scripts/m10a_acceptance.py enable \
  --destination-id we_1UB69CBLB2xuBxrayoxwttci
```

Complete the printed confirmation URL in Stripe with an email address. It is a native free
Checkout, so no card is involved. Then expire the other prepared Session:

```sh
uv run python scripts/m10a_acceptance.py expire
```

## Make the test rows eligible

The one-hour stale threshold is a production safeguard, not a useful human wait. Print the narrowly
scoped test-only D1 command and run it in the separate D1-token console:

```sh
uv run python scripts/m10a_acceptance.py d1-command
```

It adjusts `updated_at` and the due/lease fields only for the two generated Session IDs, putting
them at the front of the bounded test queue. It never writes a terminal state, a Stripe event, an
amount, or an outbox event. Stripe remains the source of the terminal facts; this only makes the
remote Cron test eligible immediately and prevents unrelated old sandbox Sessions from delaying
the proof.

Wait for the deployed hourly Cron. Then print and run the evidence query in that same D1-token
console:

```sh
uv run python scripts/m10a_acceptance.py verify-command
```

Expected outcomes are:

- expired Session: `settlement_state = expired`, `last_outcome = expired`, zero `OrderConfirmed`,
  and zero `stripe_event_count`;
- completed native-free Session: `settlement_state = confirmed`, `last_outcome = confirmed`, one
  `OrderConfirmed`, and zero `stripe_event_count`.

Finally restore the test destination:

```sh
uv run python scripts/m10a_acceptance.py enable
```

Stripe deliberately does not resend events generated while the destination was disabled. The
acceptance outcome is therefore reconciliation provenance, not a late webhook delivery.
