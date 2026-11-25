# Native customer presentation

Serve these clients on the merchant origin with the scoped `/reviews/v1/` gateway.
`/review/#invitation=...` and management fragments carry existing bearer capabilities;
they never become purchase authority. Drafts and retry keys remain tab-scoped, without
persisting the invitation/management token. Collection, moderation and the consolidated
order-notification provider remain separate from presentation. Production gates are #77.

## Host styling and states

IDs, input names and handlers stay native. Style these additive hooks rather than detecting
English text or replacing native DOM:

- `.cr-form`, `.cr-field`, `.cr-actions`, `.cr-feedback`.
- `.cr-button--primary`, `.cr-button--secondary`, `.cr-button--danger` on `.cr-button`.
- `.cr-rating`, `.cr-rating-options`, `.cr-subject-selector`, real `:disabled`, `:focus-visible`
  and selected `aria-pressed` on the connected product chooser.
- `.cr-review`, `.cr-review-body`, `.cr-review-author`, `.cr-review-verification`,
  `.cr-review-reply`, `.cr-review-reply-body`, `.cr-review-reply-author`.
- `#content[data-cr-state]`: loading, ready, invalid, unavailable, submitting, error,
  submitted or withdrawn, as applicable. `aria-busy` settles after initial loading.
- Native `data-cr-action`: retry, submit, edit, withdraw and confirm-withdraw.

The shared connected presenter preserves native draft/rating/disabled behaviour and dialog
Escape/cancel/confirm focus handling. Its destructive dialog uses native action attributes,
not text matching. The standalone native form retains its existing two-step withdrawal.

Set `window.CARTRAY_REVIEWS_UI = {contactPath:'/contact-us/'}` before loading the native
client for an actual Contact action. Only a local directory path is accepted; capability
fragments are never appended to contact links. Invalid/bare links and network/JSON failures
replace initial loading content. Read retries are disabled synchronously and late responses
cannot replace a newer form. Recovery still uses the existing supported protocol/provider;
there is no additional sender or queue introduced here.

## Public summary consumption

`public.mjs` exports `reviewsSummary(subject, fetchImpl=fetch, {signal}={})` for one validated
summary-only read. It never retrieves contributions or subject lookup. `reviewSummaryText`
formats validated independent rating/contribution counts, including singular and unrated
cases. Failure/429 is unavailable, never zero ratings.

Use **one** `createSummaryReader({maxAgeMs:30000})` per document for catalogue cards:

```js
const reader = createSummaryReader({maxAgeMs:30000});
const summary = await reader.read(subjectId, {signal: cardController.signal});
```

The host owns visibility (viewport or at most 200px near it), search generations and DOM
assignment. Do not start reads for every search match. The reader deduplicates pending and
settled subjects; fresh scroll re-entry reuses settled results. It bounds network concurrency
to two, network starts to 30 per rolling 60 seconds, cache/pending entries to 128 and freshness
to at most 60 seconds (default 30). Server admission remains authoritative; these document-local
bounds are traffic discipline, not a globally strict security rate limit.

`429 Retry-After` creates a shared reader cooldown. Invalidation does not reset cooldown or
admission history. Rejected reads expose `retryAt` where known; no background retry/polling
is scheduled. Aborting one consumer preserves another consumer's shared request. Superseded
queued reads are removed; invalidated late responses cannot populate the cache.

`observeSummaryInvalidation(reader, callback, {target:window,doc:document})` returns cleanup.
It clears matching/all cache before calling `callback({subjectId?,reason})` for native known
changes, meaningful popstate navigation, persisted page restoration, and stale focus/visibility
restoration. In the callback, invalidate your UI generation/old labels immediately, then reread
only visible mapped cards. Dispose both listeners and reader on teardown. No settled cache is
shared across ordinary new-document navigation. There is no browser-suspension freshness SLA.

## Product title and full list consistency

`attachReviews(config, document)` returns `{refresh(),dispose()}`. Keep this controller for
before-expanded-view or host navigation refresh. Refresh clears old review text immediately
and ignores superseded responses. Native changes and stale focus/restoration also revalidate.

The existing detail read emits a bubbling `cartray:reviews-snapshot` event from `#reviews`:

```js
{subjectId, state:'loading'|'ready'|'unavailable', summary:null /* or validated public summary */}
```

`ready` is emitted only after the summary and contributions have matching snapshot IDs.
Use that exact event summary for the product-title hint; do not issue another independent
summary request. Clear stale title presentation on loading/unavailable. Event data is cloned
and contains no capability. Native submit/edit/withdraw emits `cartray:reviews-changed` with
`{subjectId}` (null when the management response does not identify it).

Historical review migration, business testimonials, production SEO/withdrawal qualification
and customer release accessibility gates remain separate. Synthetic UAT reviews must remain
labelled by the merchant; public ratings are not fabricated for unmapped products.
