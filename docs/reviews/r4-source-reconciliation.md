# Effective Policy review-source reconciliation, R4 work in progress

A read-only, consistent WordPress candidate export was captured on 2026-11-06 at
02:56:26 UTC after checking the site's configured home and site URL. SHA-256:
`2f0e91e00331affb862eb5374e719e7cca8b422538c7677ead7d6816487458e9`.
The restricted export and its per-record disposition manifest contain private
source data and must not be checked into Git. This capture is a candidate, not
an approved complete migration snapshot: attachment and source-writer coverage,
source settings, classification and subject mapping still require reconciliation.

The candidate contains 125 rows: 17 approved reviews, six approved replies, six
pending reviews and 96 spam reviews. No review row has a rejected status in this
capture; WordPress's stored statuses must not be inferred to distinguish every
moderation action. Fifteen approved reviews have five stars and two have no
rating. Six approved reviews have vote metadata. Eight approved reviews carry a
source verified flag; this is a legacy assertion and does not prove independent
CartRay verification. Three approved reviews carry `title` metadata and one
carries `cr_rev_title`; these fields need an explicit preservation and conflict
check before import. The 24 media attachment URL/type pairs in the capture are
associated with spam, and remain restricted. Existing standalone editorial
testimonial videos are not review records.

The approved review roots and their legacy post identities are:

| WordPress post | Approved review IDs | Initial classification |
| --- | --- | --- |
| 22 | 4171, 4172, 4173, 4174, 4175, 4176, 4177, 4215, 4384, 4450 | Policies page contained service, audit-support and general business experiences. Do not assign these to a specific product or emit product structured data without evidence. |
| 195 | 4375 | Product subject; canonical CartRay key and URL require confirmation. |
| 264 | 4414 | Product subject; canonical CartRay key and URL require confirmation. |
| 270 | 3734, 3736 | Product subject; canonical CartRay key and URL require confirmation. |
| 477 | 7069 | Product subject, unrated; canonical CartRay key and URL require confirmation. |
| 4067 | 7142 | Product subject, unrated; canonical CartRay key and URL require confirmation. |
| 5283 | 3987 | Product subject; canonical CartRay key and URL require confirmation. |

Approved reply-to-parent mappings are 4417→4215, 4418→4174, 4419→4171,
4420→3987, 4421→3736 and 4422→3734. The restricted manifest accounts for
all candidate rows individually. Pending and spam rows are not publication
candidates. The source option `ivole_review_forms[0].rev_perm` was `anybody`
at capture; this alone is not proof of effective frontend, plugin-filter or
writer behavior. A later read-only plugin inventory reported Customer Reviews
for WooCommerce, Trustpilot Reviews and WooCommerce Product Reviews Pro as
active. Their effective writer paths and stored contribution types still need
reconciliation; active plugins are not evidence that every feature is used. A
later read-only aggregate found only `review`, `comment`, and `order_note` comment
types; the 24 comments outside review parentage were attached to blog posts (three
approved and 21 spam). This corroborates the captured selection but is not part of
the same consistent snapshot and cannot by itself establish extraction completeness.

Read-only inspection of the installed Customer Reviews handler establishes an
actual unauthenticated `cr_submit_review` AJAX writer registration. Its handler
resolves permissions through the default form settings and writes a WordPress `review` comment with `wp_new_comment`. The installed settings
resolver uses the first form's `rev_perm`, falling back to the older AJAX option
only when that value is absent. The captured first-form value is `anybody`, so
that handler's configured permission permits guest submission; runtime filters,
frontend availability and other writers still need verification. No submission
was made. The separate authenticated CusRev review REST writer and other active
plugins also remain relevant to any eventual cutover.

Remaining source qualification includes complete attachment and comment-graph
coverage, current effective settings and writer paths, a decision for each of
the ten page-associated experiences, product keys and canonical URLs, source
verification and voting semantics, and privacy review of public prose. The
checked-in CartRay real-test catalogue currently contains two test products,
which is not a complete authoritative mapping for these subjects. No records
have been imported or published by this investigation.

## Provisional development crosswalk

A separate source review supplied provisional keys for exercising the adapter:
`PROVISIONAL-WP-195`, `PROVISIONAL-WP-264`, `PROVISIONAL-WP-270`,
`PROVISIONAL-WP-477`, `PROVISIONAL-WP-4067`, and `PROVISIONAL-WP-5283`.
These are test labels, not CartRay commerce keys or approved production aliases.
The candidate URL paths use the WordPress product slugs in the source. The
handoff reports direct public-page observations for 195 and 264, search-result
support for 270 and 5283, unconfirmed reachability for 477, and conflicting older
public evidence for 4067. The latter two require particular care around retired,
renamed, or changed products. All keys and canonical URLs remain unapproved.

For a private local rehearsal only, these six labels plus a provisional
store-level label for page 22 produced seven subjects, 17 approved roots, six
replies and two unrated roots from the pinned candidate export. This exploratory
label for page 22 is not a final classification. The adapter explicitly marked
the resulting draft incomplete, and the importer rejected it as required. No
real source content was imported into D1 or published.

A second read-only export on 2026-11-06 at 03:47:09 UTC used a recursive
traversal of all review descendants within one repeatable-read transaction.
It returned 125 comment rows and 338 metadata rows. The comment, metadata and
subject rows matched the earlier capture exactly; the source still contained
17 approved roots, six approved replies, six pending roots and 96 spam roots.
The second restricted export SHA-256 is
`a1f268855d4531f7f9c5756cfa582d092400f91a9ae3553b9eac0f30de69b60a`.
This corroborates the captured graph, but does not determine the effective
settings or establish permission to publish the source content.

A third recursive export at 03:49:41 UTC additionally checked the source's home
and site URLs inside the transaction, verified the four queried WordPress tables
use InnoDB, and enforced capture limits. Its 125 comment rows, 338 metadata rows
and subject rows again matched the preceding recursive export. Its restricted
SHA-256 is `eb5e5786075ae13e9312879cc1a40c7b2a1c1b8489bbc88892c46481457386a8`.
The exporter still marks the candidate incomplete pending reconciliation of
plugin-specific behavior and the other publication checks.
A fourth capture at 03:50:46 UTC used explicit standard WordPress columns and
again matched the preceding comment, metadata and subject rows. Its restricted
SHA-256 is `9d4c8c427aa9469fd4f7e918b6f9b7e51a970abe7f065ef045a88ff18d1ed66c`.

The installed WooCommerce Product Reviews Pro code recognizes separate comment
types for review contributions, replies, photos, videos and questions. The
source-wide comment-type inventory found none of those non-review contribution
types at the later check. The exporter now fails closed if those known types or
CusRev's Q&A type appear, rather than silently leaving new records out. A fifth
capture at 03:55:47 UTC passed that check and matched the preceding rows; its
restricted SHA-256 is `2c617455fcc785c1dbfa87bce7885e4af82c50995f41b456283778bb40e5760d`.
External testimonial videos remain editorial content and are not inferred from
this check.

A sixth capture at 03:57:08 UTC checked all stored comment types within the
transaction. Only `review`, `comment` and `order_note` were present; comments
outside the review graph were blog comments, and order notes belonged to order
or subscription posts. Unknown types and unclassified comments now fail closed
instead of being silently omitted. The restricted SHA-256 is
`01dfa3ab08d21018bbf8badf393699499e392458039a73ed36eb3b878e94f8ec`;
its selected rows again match the prior captures. This does not assert anything
about third-party content or remote provider-held records.
A seventh capture at 03:58:11 UTC also rejected known review-related metadata on
off-graph records whose stored comment type is `comment`. It passed and returned the same
selected rows; the restricted SHA-256 is
`cd4260ab06f2fbeaf689c36298467e76ccd3934b394de7d4f01d3c623a57e167`.
This metadata check is not proof about unrecognized plugin metadata or external
review providers.

An eighth restricted capture at 04:11:10 UTC recorded only whether each source
post has a password, without exporting passwords. None of the 26 referenced
subjects was password-protected, and comment and metadata rows again matched the
prior capture. Its SHA-256 is
`81c8677661e42b1c8e1e22a711d4e9cb4ed1b11dda3789088a87d5e71d8c747c`.
Published status and lack of a password do not establish visibility under
catalogue or access-control plugins.

The optional reconciliation command was exercised against this capture with the
provisional crosswalk. It generated restricted dispositions for all 125 records,
338 metadata rows and 26 subjects, independently compared candidate text, dates,
ratings, vote totals and verification claims, and left the snapshot explicitly
incomplete. The source export, candidate and per-row report remain outside Git.

The independent privacy check found source-author email addresses embedded in
the bodies of review records 4384 and 4450. The optional test-only redaction
mode replaced those exact occurrences with `[email removed]` in the restricted draft
and recorded the affected IDs and fields without recording addresses in the
report. Original text remains in the restricted source export. The resulting
candidate remains incomplete and requires prose and publication review.
