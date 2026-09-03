# Milestone 9 contract: terminal Checkout return experience

## Scope

M9 improves the static storefront after a Stripe-hosted Checkout return. It keeps Stripe's signed
webhook as the only settlement authority and CartRay's tiny D1-backed status route as the only
browser return signal. It does not create a customer order API, call Stripe from the browser,
grant fulfilment, change Valet, add accounts, or enable live mode.

This implements the customer-return follow-up tracked in
[#23](https://github.com/mitchins/cart-ray/issues/23).

## Return-state behaviour

| Observed return state | Cart | Checkout controls | Customer action |
| --- | --- | --- | --- |
| `confirmed`, matching unchanged pending checkout | Clear persisted cart and consume correlation | Immediately enabled | Add products and start a fresh Checkout without reload |
| `confirmed`, cart changed in another tab | Keep current cart; consume only the matching old correlation | Immediately enabled | Continue with the current cart without losing it |
| `checkout=cancelled` | Keep cart | Enabled | Resume or alter cart and Checkout again |
| `pending` | Keep cart and pending correlation | Locked | Retry the same confirmation route; no duplicate Checkout |
| status unavailable/unknown | Keep cart and pending correlation | Locked | Retry the same confirmation route; no duplicate Checkout |

A confirmed return removes **all** Checkout query parameters from the visible URL, not merely the
session ID. The confirmation message makes clear whether CartRay cleared the matching cart or
preserved a cart changed elsewhere. Product-add and Checkout handlers are already bound before
the return is resolved, so an unlocked confirmed or cancelled view is actually capable of a new
purchase rather than merely appearing active.

## Safety invariants

1. The browser cannot settle, confirm, or grant access; it still only polls CartRay's existing
   opaque Session status signal.
2. A stale or foreign Session URL cannot clear a cart, and a cart changed in another tab survives
   a confirmed older Checkout.
3. An uncertain state never unlocks Checkout. This avoids a second purchase while the original
   payment may still settle.
4. Cancellation remains non-authoritative: it retains the cart, but its normal return path is
   immediately usable for a new Checkout.
5. Return status is announced through the existing polite live region and the action label states
   whether the customer can continue shopping or should recheck confirmation.

## Cancellation is not expiry

`checkout=cancelled` records browser/customer intent only. It permits the retained local cart to
be used again, but it does not change the original Stripe Checkout Session or CartRay's D1
settlement state. If the customer later completes that older Stripe Session, its valid signed
`checkout.session.completed` webhook is still processed normally.

This deliberately leaves a small duplicate-purchase possibility when a customer cancels, starts a
replacement Checkout, then completes the original Session. M9 does not imply that a cancel return
is a Stripe terminal state. Authoritative Stripe expiry ingestion and replacement-Session
semantics are separate follow-up work; until then, only `confirmed` is an authoritative terminal
state visible to this status route.

## Acceptance criteria

M9 tests prove that:

1. matching confirmed Checkout clears the cart, removes the return query, unlocks controls, and
   can create a fresh Checkout immediately;
2. a confirmed older Checkout retains a changed current cart but unlocks that cart for its own
   purchase;
3. cancellation is clearly announced while leaving the cart and Checkout path usable;
4. pending and unavailable/unknown outcomes retain the cart, preserve the lock, and provide only
   a retry-confirmation action; and
5. a cancelled browser return unlocks the retained cart without changing CartRay's settlement
   state, and a later valid completion webhook remains processable; and
6. the browser contract, webhook-only settlement authority, and existing cross-tab protections
   remain unchanged.
