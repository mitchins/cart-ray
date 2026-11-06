// R5 consumes the public R4 projection, never the Reviews write model.
// The same normalized object drives HTML and JSON-LD.
const ID = /^[A-Za-z0-9_-]{1,128}$/;
const PUBLIC_ID = /^[A-Za-z0-9_-]{1,130}$/;
const PATH = /^\/(?:[A-Za-z0-9_-]+\/)*$/;
const MAX_REVIEWS = 200;
const text = (value, max) => typeof value === 'string' && value.length <= max;
const escapeHtml = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');

export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  // Canonical hashes require deterministic UTF-16 code-unit ordering, not locale-dependent collation.
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'; // NOSONAR: locale-dependent sorting would change approved digests.
  return JSON.stringify(value);
}
export async function sha256(value) {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
function review(item, ids, anchorsSeen, lineageSeen, depth = 0) {
  if (!item || typeof item.id !== 'string' || !PUBLIC_ID.test(item.id) || ids.has(item.id) || anchorsSeen.has(item.id) || depth > 4 || !(item.author == null || text(item.author, 160)) || !(item.body == null || text(item.body, 10000)) ||
      !(item.rating === null || Number.isInteger(item.rating) && item.rating >= 1 && item.rating <= 5) || !Array.isArray(item.replies) || item.replies.length > 40) throw new Error('invalid public contribution');
  if (ids.size >= MAX_REVIEWS) throw new Error('too many contributions');
  ids.add(item.id); anchorsSeen.add(item.id);
  const kind = item.source?.kind;
  if (!['native', 'historical'].includes(kind)) throw new Error('missing public source');
  const sourceId = item.source_id == null ? item.id : item.source_id;
  if (typeof sourceId !== 'string' || !ID.test(sourceId)) throw new Error('invalid source identity');
  if (item.source_id != null && item.id !== `${kind === 'native' ? 'n' : 'h'}-${sourceId}`) throw new Error('invalid source identity');
  if (kind === 'native' && !/^[a-f0-9]{64}$/.test(sourceId)) throw new Error('invalid native identity');
  const lineage = [`${kind}:${sourceId}`];
  if (item.historical_provenance != null) {
    const historicalId = item.historical_provenance.source_id;
    if (kind !== 'native' || typeof historicalId !== 'string' || !ID.test(historicalId)) throw new Error('invalid historical identity');
    lineage.push(`historical:${historicalId}`);
  }
  const historical = item.historical_provenance || null;
  for (const key of lineage) {
    if (lineageSeen.has(key)) throw new Error('duplicate source identity');
    lineageSeen.add(key);
  }
  const rawDate = item.date ?? historical?.date ?? null;
  if (rawDate !== null && (typeof rawDate !== 'object' || Array.isArray(rawDate) || !text(rawDate.original, 100) || !text(rawDate.precision, 32) || !text(rawDate.timezone_provenance, 80))) throw new Error('invalid historical date');
  const date = rawDate === null ? null : Object.fromEntries(['original', 'precision', 'timezone_provenance', 'utc', 'source_local_raw', 'source_utc_raw'].filter((key) => key in rawDate).map((key) => {
    const value = rawDate[key];
    if (value !== null && !text(value, 100)) throw new Error('invalid historical date field');
    return [key, value];
  }));
  const createdAt = item.created_at == null ? null : item.created_at;
  if (createdAt !== null && (!Number.isSafeInteger(createdAt) || createdAt < 0 || createdAt > 253402300799)) throw new Error('invalid creation time');
  const anchors = item.anchors || item.historical_provenance?.anchors || [];
  if (!Array.isArray(anchors) || anchors.length > 20 || anchors.some((anchor) => typeof anchor !== 'string' || !/^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/.test(anchor))) throw new Error('invalid anchors');
  for (const anchor of anchors) {
    if (anchorsSeen.has(anchor)) throw new Error('duplicate anchor');
    anchorsSeen.add(anchor);
  }
  const source = item.source?.attribution || historical?.source?.attribution || null;
  const votesRaw = item.votes ?? historical?.votes ?? null;
  if (votesRaw !== null && (!votesRaw || typeof votesRaw !== 'object' || ['up', 'down', 'net'].some((key) => !(votesRaw[key] === null || Number.isSafeInteger(votesRaw[key]))) || (votesRaw.up !== null && votesRaw.up < 0) || (votesRaw.down !== null && votesRaw.down < 0))) throw new Error('invalid votes');
  const votes = votesRaw === null ? null : { up: votesRaw.up, down: votesRaw.down, net: votesRaw.net };
  if (votes && votes.up !== null && votes.down !== null && votes.net !== null && votes.up - votes.down !== votes.net) throw new Error('inconsistent votes');
  const allowedVerification = [null, 'cartray_verified_acquisition', 'legacy_woo_verified_assertion', 'legacy_cusrev_assertion', 'email_only', 'unverified'];
  const verification = item.verification ?? null;
  if (!allowedVerification.includes(verification)) throw new Error('invalid verification');
  const historicalVerification = historical?.verification ?? null;
  if (!allowedVerification.includes(historicalVerification)) throw new Error('invalid historical verification');
  if (source !== null && !text(source, 300)) throw new Error('invalid attribution');
  return { id: item.id, lineage, author: item.author || '', body: item.body || '', rating: item.rating, date, created_at: createdAt, anchors, source, votes, historical_verification: historicalVerification,
    verification,
    title: item.title == null ? null : text(item.title, 300) ? item.title : (() => { throw new Error('invalid title'); })(),
    replies: item.replies.map((entry) => review(entry, ids, anchorsSeen, lineageSeen, depth + 1)) };
}
export function normalizeProjection(input) {
  const subject = input?.subject, summary = input?.summary, pages = input?.pages;
  if (!subject || typeof subject.id !== 'string' || !ID.test(subject.id) || !['product', 'service', 'store'].includes(subject.kind) ||
      !text(subject.name, 160) || !subject.name || !text(subject.canonical_path, 2048) || !PATH.test(subject.canonical_path) ||
      !summary || summary.subject_id !== subject.id || !text(summary.snapshot_id, 200) || !summary.snapshot_id ||
      !Array.isArray(pages) || pages.length === 0 || pages.length > 20) throw new Error('invalid publication subject');
  const ids = new Set(), anchorsSeen = new Set(), lineageSeen = new Set(), reviews = []; let cursor = null;
  for (const page of pages) {
    if (!page || page.subject_id !== subject.id || page.snapshot_id !== summary.snapshot_id || page.request_after !== cursor ||
        !Array.isArray(page.contributions) || typeof page.has_more !== 'boolean') throw new Error('inconsistent projection');
    reviews.push(...page.contributions.map((item) => review(item, ids, anchorsSeen, lineageSeen)));
    if (reviews.length > MAX_REVIEWS || (page.has_more && (!PUBLIC_ID.test(page.next_after || '') || !page.contributions.some((item) => item.id === page.next_after))) || (!page.has_more && page.next_after !== null)) throw new Error('invalid pagination');
    cursor = page.has_more ? page.next_after : null;
  }
  if (pages.at(-1).has_more || pages.slice(0, -1).some((page) => !page.has_more)) throw new Error('incomplete projection');
  const ratings = reviews.map((item) => item.rating).filter((rating) => rating !== null);
  const average = ratings.length ? ratings.reduce((sum, rating) => sum + rating, 0) / ratings.length : null;
  if (!summary.rating || summary.rating.rated_count !== ratings.length || summary.contribution_count !== reviews.length ||
      (average === null ? summary.rating.average !== null : typeof summary.rating.average !== 'number' || Math.abs(summary.rating.average - average) > 1e-9)) throw new Error('summary mismatch');
  return { schema: 'cartray.reviews.publication.v1', subject: { id: subject.id, kind: subject.kind, name: subject.name, canonical_path: subject.canonical_path },
    source_snapshot: summary.snapshot_id, rating: { average, rated_count: ratings.length }, contribution_count: reviews.length, reviews };
}
export function renderPublication(publication, origin) {
  const { subject, rating, reviews } = publication;
  const originUrl = new URL(origin);
  if (originUrl.protocol !== 'https:' || originUrl.username || originUrl.password || originUrl.pathname !== '/' || originUrl.search || originUrl.hash) throw new Error('invalid canonical origin');
  const verificationLabel = (value) => ({ cartray_verified_acquisition: 'Verified acquisition', legacy_woo_verified_assertion: 'Verified by the previous provider', legacy_cusrev_assertion: 'Verified by the previous provider' })[value] || null;
  const render = (item) => {
    const label = verificationLabel(item.verification), legacy = verificationLabel(item.historical_verification);
    const date = item.date?.original || (item.created_at === null ? null : new Date(item.created_at * 1000).toISOString());
    const votes = item.votes ? [item.votes.up === null ? null : `${item.votes.up} helpful`, item.votes.down === null ? null : `${item.votes.down} not helpful`].filter(Boolean).join(' · ') || (item.votes.net === null ? '' : `Net votes: ${item.votes.net}`) : '';
    return `<article id="${escapeHtml(item.id)}">${item.anchors.map((anchor) => `<span id="${escapeHtml(anchor)}"></span>`).join('')}${item.title ? `<h3>${escapeHtml(item.title)}</h3>` : ''}<p>${escapeHtml(item.author)}</p>${item.rating === null ? '' : `<p>${item.rating} out of 5</p>`}${date ? `<p>${escapeHtml(date)}</p>` : ''}${item.source ? `<p>Source: ${escapeHtml(item.source)}</p>` : ''}${label ? `<p>${escapeHtml(label)}</p>` : ''}${legacy ? `<p>Historical record: ${escapeHtml(legacy)}</p>` : ''}${votes ? `<p>${escapeHtml(votes)}</p>` : ''}<p>${escapeHtml(item.body)}</p>${item.replies.map(render).join('')}</article>`;
  };
  const aggregate = subject.kind === 'product' && rating.rated_count ? { '@context': 'https://schema.org', '@type': 'Product', name: subject.name,
    url: new URL(subject.canonical_path, origin).href, aggregateRating: { '@type': 'AggregateRating', ratingValue: rating.average, ratingCount: rating.rated_count, bestRating: 5, worstRating: 1 } } : null;
  const jsonld = aggregate ? JSON.stringify(aggregate).replaceAll('<', '\\u003c') : '';
  const lease = `const clearReviews=()=>{document.querySelector('main')?.replaceChildren(document.createTextNode('Reviews are refreshing.'));document.querySelector('script[type=\"application/ld+json\"]')?.remove()};setTimeout(()=>{clearReviews();location.reload()},30000);addEventListener('pagehide',clearReviews);` ;
  const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">${jsonld ? `<script type="application/ld+json">${jsonld}</script>` : ''}</head><body><main><h1>${escapeHtml(subject.name)}</h1><p>${rating.rated_count ? `${rating.average.toFixed(1)} from ${rating.rated_count} ratings` : 'No ratings yet'} · ${publication.contribution_count} contributions</p>${reviews.map(render).join('')}</main><script>${lease}</script></body></html>`;
  return { html, jsonld };
}

// The approval identity binds the exact rendered bytes as well as input data.
export function publicationArtifact(projection, media, canonicalOrigin, sourceOrigin, sourceScope) {
  const { html, jsonld } = renderPublication(projection, canonicalOrigin);
  return { schema: 'cartray.reviews.artifact.v1', projection, media, canonical_origin: canonicalOrigin,
    source_origin: sourceOrigin, source_scope: sourceScope, html, jsonld };
}
