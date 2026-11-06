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
writer behavior.

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
