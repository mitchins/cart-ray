// Public reads only; deployments must route /reviews/v1/ to the scoped Reviews service.
const ID = /^[a-z0-9-]{1,128}$/;
const MAX_BYTES = 128_000;

async function readJson(url, fetchImpl, signal) {
  const response = await fetchImpl(url, { signal, cache: 'no-store', redirect: 'error', credentials: 'omit' });
  if (!response.ok || !response.body) throw new Error('Reviews unavailable');
  const reader = response.body.getReader();
  const chunks = []; let total = 0;
  try {
    while (true) {
      const part = await reader.read(); if (part.done) break;
      total += part.value.byteLength;
      if (total > MAX_BYTES) throw new Error('Reviews response too large');
      chunks.push(part.value);
    }
  } catch (error) {
    void reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const part of chunks) { bytes.set(part, offset); offset += part.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}

export async function reviewsPage(subject, cursor = null, snapshot = null, fetchImpl = fetch) {
  if (!ID.test(subject) || (cursor !== null && (!/^[A-Za-z0-9_-]{1,130}$/.test(cursor) || typeof snapshot !== 'string'))) throw new Error('Invalid review selection');
  const query = new URLSearchParams({ limit: '10' });
  if (cursor !== null) { query.set('after', cursor); query.set('snapshot', snapshot); }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const base = `/reviews/v1/subjects/${encodeURIComponent(subject)}`;
    const [summary, page] = await Promise.all([
      readJson(`${base}/summary`, fetchImpl, controller.signal),
      readJson(`${base}/contributions?${query}`, fetchImpl, controller.signal),
    ]);
    if (summary.subject_id !== subject || page.subject_id !== subject || typeof summary.snapshot_id !== 'string' || page.snapshot_id !== summary.snapshot_id ||
        (snapshot !== null && snapshot !== summary.snapshot_id) || !Array.isArray(page.contributions) || page.contributions.length > 10 ||
        typeof page.has_more !== 'boolean' || (page.has_more && !/^[A-Za-z0-9_-]{1,130}$/.test(page.next_after || '')) ||
        !summary.rating || !Number.isSafeInteger(summary.rating.rated_count) || !Number.isSafeInteger(summary.contribution_count) ||
        !(summary.rating.average === null || (typeof summary.rating.average === 'number' && summary.rating.average >= 1 && summary.rating.average <= 5))) throw new Error('Reviews changed');
    return { summary, page };
  } finally { clearTimeout(timer); controller.abort(); }
}

function line(doc, tag, text, parent) { const node = doc.createElement(tag); node.textContent = text; parent.append(node); return node; }
export function renderContribution(item, parent, doc, budget) {
  // The public API caps historical contributions at 200 and returns at most
  // one native staff reply per result. An explicit stack handles deep trees.
  const pending = [{ item, parent, root: true }];
  while (pending.length) {
    const next = pending.pop(), value = next.item;
    if (!value || typeof value !== 'object' || typeof value.body !== 'string' || typeof value.author !== 'string' || !Array.isArray(value.replies) || --budget.remaining < 0 || value.replies.length + pending.length > budget.remaining) throw new Error('Invalid contribution');
    const node = doc.createElement(next.root ? 'article' : 'blockquote');
    if (next.root) {
      if (typeof value.title === 'string' && value.title) line(doc, 'h3', value.title, node);
      line(doc, 'p', `${value.author || 'Anonymous'}${Number.isInteger(value.rating) && value.rating >= 1 && value.rating <= 5 ? ` · ${value.rating} of 5 stars` : ''}`, node);
      if (value.verification === 'cartray_verified_acquisition') line(doc, 'p', 'Verified purchase', node);
      line(doc, 'p', value.body, node);
    } else {
      line(doc, 'p', value.body, node); line(doc, 'footer', value.author || 'Store', node);
    }
    next.parent.append(node);
    for (let i = value.replies.length - 1; i >= 0; i--) pending.push({ item: value.replies[i], parent: node, root: false });
  }
}

export function attachReviews(config, doc = document) {
  const subject = config?.reviewSubjects?.[config.productKey];
  if (!config?.reviewsEnabled || !subject || !ID.test(subject)) return;
  const section = doc.querySelector('#reviews'), status = doc.querySelector('#reviews-status');
  const list = doc.querySelector('#review-list'), more = doc.querySelector('#reviews-more');
  if (!section || !status || !list || !more) return;
  section.hidden = false;
  let cursor = null, snapshot = null, busy = false;
  async function load() {
    if (busy) return;
    busy = true; more.disabled = true; status.textContent = 'Loading reviews…';
    try {
      const result = await reviewsPage(subject, cursor, snapshot);
      const fragment = doc.createDocumentFragment();
      const budget = { remaining: 220 }; // 200 historical + 10 native reviews + their replies.
      for (const item of result.page.contributions) renderContribution(item, fragment, doc, budget);
      list.append(fragment);
      const rating = result.summary.rating;
      status.textContent = rating.rated_count ? `${rating.average} out of 5 · ${rating.rated_count} ratings · ${result.summary.contribution_count} contributions` : `No ratings yet · ${result.summary.contribution_count} contributions`;
      cursor = result.page.next_after; snapshot = result.summary.snapshot_id;
      more.hidden = !result.page.has_more;
    } catch {
      // A stale snapshot invalidates all previously displayed text, not just the new page.
      list.replaceChildren(); cursor = null; snapshot = null; more.hidden = true;
      status.textContent = 'Reviews are temporarily unavailable. Please refresh to try again.';
    } finally { busy = false; more.disabled = false; }
  }
  more.addEventListener('click', () => { void load(); });
  void load();
}

if (typeof document !== 'undefined') attachReviews(window.CARTRAY_STOREFRONT);
