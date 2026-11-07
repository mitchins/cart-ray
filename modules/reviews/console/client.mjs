// Same-origin only: the trusted access proxy supplies the independently verified assertion.
const ID = /^[a-f0-9]{64}$/;
const REQUEST_KEY = /^[A-Za-z0-9_-]{1,128}$/;
const SUBJECT = /^[A-Za-z0-9_-]{1,128}$/;
export class ConsoleError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}
function entry(item) {
  if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') ||
      typeof item.subject_id !== 'string' || item.subject_id.length > 128 ||
      ![null, undefined].includes(item.rating) && (!Number.isInteger(item.rating) || item.rating < 1 || item.rating > 5) ||
      ['author', 'title', 'body', 'subject_name'].some((key) => item[key] != null && typeof item[key] !== 'string')) {
    throw new ConsoleError('The review service returned an invalid response.');
  }
  return { review_id: item.review_id, revision_id: item.revision_id, subject_id: item.subject_id,
    subject_name: item.subject_name || '', author: item.author || '', title: item.title || '', body: item.body || '', rating: item.rating ?? null };
}
const UNCERTAIN = 'We could not confirm whether the decision completed. Refresh the queue before trying again.';
async function responseJson(response, mutation = false) {
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) throw new ConsoleError('Your staff session is unavailable or does not have permission. Sign in again or contact an administrator.', response.status);
    if (response.status === 409) throw new ConsoleError('This review changed. Refresh the queue before trying again.', response.status);
    if (response.status === 429) throw new ConsoleError('Too many requests. Please wait a moment and try again.', response.status);
    throw new ConsoleError(mutation && response.status >= 500 ? UNCERTAIN : 'The review service is temporarily unavailable. Please try again.', response.status);
  }
  try { return await response.json(); } catch { throw new ConsoleError(mutation ? UNCERTAIN : 'The review service returned an invalid response.'); }
}
export function createClient(fetcher = fetch, timeoutMs = 8000) {
  async function request(url, options, mutation = false) {
    const controller = new AbortController();
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => { controller.abort(); reject(new ConsoleError(mutation ? UNCERTAIN : 'The review service timed out. Please try again.')); }, timeoutMs);
    });
    try {
      return await Promise.race([(async () => responseJson(await fetcher(url, { ...options, signal: controller.signal }), mutation))(), timeout]);
    } catch (error) {
      if (error instanceof ConsoleError) throw error;
      throw new ConsoleError(mutation ? UNCERTAIN : 'The review service is temporarily unavailable. Please try again.');
    } finally { clearTimeout(timer); }
  }
  async function list(kind, after = null) {
    if (after !== null && !ID.test(after)) throw new ConsoleError('Invalid queue cursor. Refresh the queue.');
    const data = await request(`/reviews-admin/v1/${kind}` + (after ? `?after=${after}` : ''), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!data || !Array.isArray(data[kind]) || data[kind].length > 50 || !(data.next_after === null || ID.test(data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
    const records = data[kind].map(entry);
    if (new Set(records.map((item) => item.review_id)).size !== records.length || (data.next_after && !records.some((item) => item.review_id === data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
    return { records, next_after: data.next_after };
  }
  return {
    async pending(after = null) {
      const page = await list('pending', after); return { pending: page.records, next_after: page.next_after };
    },
    async published(after = null) { const page = await list('published', after); return { published: page.records, next_after: page.next_after }; },
    async history(reviewId, kind, after = null) {
      if (!ID.test(reviewId || '') || !['revisions', 'audit'].includes(kind) || (after !== null && !ID.test(after))) throw new ConsoleError('Invalid history request. Refresh the queue.');
      const data = await request(`/reviews-admin/v1/contributions/${reviewId}/${kind}` + (after ? `?after=${after}` : ''), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      const key = kind === 'audit' ? 'event_id' : 'revision_id';
      if (!data || data.review_id !== reviewId || !Array.isArray(data[kind]) || data[kind].length > 50 || !(data.next_after === null || ID.test(data.next_after)) || data[kind].some((item) => !item || !ID.test(item[key] || '')) || (data.next_after && !data[kind].some((item) => item[key] === data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
      if (new Set(data[kind].map((item) => item[key])).size !== data[kind].length || data[kind].some((item) => kind === 'audit'
        ? typeof item.action !== 'string' || typeof item.actor !== 'string' || !Number.isSafeInteger(item.occurred_at) || !ID.test(item.revision_id || '')
        : ['author', 'title', 'body'].some((field) => item[field] != null && typeof item[field] !== 'string') || !(item.rating == null || Number.isInteger(item.rating) && item.rating >= 1 && item.rating <= 5))) throw new ConsoleError('The review service returned an invalid response.');
      return { records: data[kind], next_after: data.next_after };
    },
    async replyHistory(item, after = null) {
      if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') || (after !== null && !ID.test(after))) throw new ConsoleError('Invalid reply history request. Refresh the list.');
      const data = await request(`/reviews-admin/v1/contributions/${item.review_id}/reply-revisions` + (after ? `?after=${after}` : ''), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!data || data.review_id !== item.review_id || data.state !== 'published' || data.current_revision_id !== item.revision_id) throw new ConsoleError('This review changed. Refresh the list before trying again.', 409);
      const head = data.reply;
      if (head !== null && (!head || !ID.test(head.reply_id || '') || !ID.test(head.reply_revision_id || '') || !ID.test(head.parent_revision_id || '') || !['published', 'withdrawn'].includes(head.state))) throw new ConsoleError('The review service returned an invalid response.');
      const rows = data.reply_revisions;
      if (!Array.isArray(rows) || rows.length > 50 || !(data.next_after === null || ID.test(data.next_after)) || rows.some((row) => !row || !ID.test(row.reply_revision_id || '') || typeof row.author !== 'string' || typeof row.body !== 'string' || !Number.isSafeInteger(row.created_at)) || new Set(rows.map((row) => row.reply_revision_id)).size !== rows.length || (data.next_after && !rows.some((row) => row.reply_revision_id === data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
      return { head, records: rows.map(({ reply_revision_id, author, body, created_at }) => ({ reply_revision_id, author, body, created_at })), next_after: data.next_after };
    },
    async replyAudit(item, after = null) {
      if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') || (after !== null && !ID.test(after))) throw new ConsoleError('Invalid reply audit request. Refresh the list.');
      const data = await request(`/reviews-admin/v1/contributions/${item.review_id}/reply-audit` + (after ? `?after=${after}` : ''), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!data || data.review_id !== item.review_id || data.state !== 'published' || data.current_revision_id !== item.revision_id) throw new ConsoleError('This review changed. Refresh the list before trying again.', 409);
      const rows = data.reply_audit;
      if (!Array.isArray(rows) || rows.length > 50 || !(data.next_after === null || ID.test(data.next_after)) || rows.some((row) => !row || !ID.test(row.event_id || '') || !ID.test(row.reply_id || '') || !ID.test(row.reply_revision_id || '') || typeof row.actor !== 'string' || typeof row.action !== 'string' || !Number.isSafeInteger(row.occurred_at) || row.occurred_at < 0 || row.occurred_at > 253402300799) || new Set(rows.map((row) => row.event_id)).size !== rows.length || (data.next_after && !rows.some((row) => row.event_id === data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
      return { records: rows.map(({ event_id, actor, action, occurred_at }) => ({ event_id, actor, action, occurred_at })), next_after: data.next_after };
    },
    async reply(item, action, payload, key) {
      if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') || !['upsert', 'withdraw'].includes(action) || !REQUEST_KEY.test(key || '') || !payload || !(payload.expected_reply_revision_id === null || ID.test(payload.expected_reply_revision_id || ''))) throw new ConsoleError('Invalid reply request. Refresh the list.');
      if (action === 'withdraw' && payload.expected_reply_revision_id === null) throw new ConsoleError('Invalid reply request. Refresh the list.');
      if (action === 'upsert' && (typeof payload.author !== 'string' || !payload.author.trim() || payload.author.length > 120 || typeof payload.body !== 'string' || payload.body.trim().length < 3 || payload.body.length > 5000)) throw new ConsoleError('Enter a display name and a reply of at least three characters.');
      const body = { parent_revision_id: item.revision_id, expected_reply_revision_id: payload.expected_reply_revision_id };
      if (action === 'upsert') { body.author = payload.author; body.body = payload.body; }
      const data = await request(`/reviews-admin/v1/contributions/${item.review_id}/${action === 'upsert' ? 'reply' : 'reply-withdraw'}`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(body) }, true);
      if (!data || !ID.test(data.reply_id || '') || !ID.test(data.reply_revision_id || '') || data.state !== (action === 'upsert' ? 'published' : 'withdrawn')) throw new ConsoleError(UNCERTAIN);
      return { reply_id: data.reply_id, reply_revision_id: data.reply_revision_id, state: data.state };
    },
    async deliveries(state, after = null) {
      if (!['uncertain', 'dispatching', 'failed', 'accepted', 'revoked'].includes(state) || (after !== null && !ID.test(after))) throw new ConsoleError('Invalid delivery filter.');
      const data = await request(`/reviews-admin/v1/deliveries?state=${state}` + (after ? `&after=${after}` : ''), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!data || data.state !== state || !Array.isArray(data.deliveries) || data.deliveries.length > 50 || !(data.next_after === null || ID.test(data.next_after)) || data.deliveries.some((row) => !row || !ID.test(row.delivery_identity || '') || !ID.test(row.generation_id || '') || !SUBJECT.test(row.acquisition_id || '') || row.state !== state || !Number.isSafeInteger(row.prepared_at) || row.prepared_at < 0 || row.prepared_at > 253402300799) || new Set(data.deliveries.map((row) => row.delivery_identity)).size !== data.deliveries.length || (data.next_after && !data.deliveries.some((row) => row.delivery_identity === data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
      return { records: data.deliveries.map(({ acquisition_id, delivery_identity, generation_id, state: rowState, prepared_at }) => ({ acquisition_id, delivery_identity, generation_id, state: rowState, prepared_at })), next_after: data.next_after };
    },
    async refreshDeliveryEvidence(deliveryId) {
      if (!ID.test(deliveryId || '')) throw new ConsoleError('Invalid delivery reference.');
      const data = await request(`/reviews-admin/v1/deliveries/${deliveryId}/refresh`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: '{}' }, true);
      if (data?.status !== 'refreshed') throw new ConsoleError('Provider evidence is unavailable.');
    },
    async acknowledgeDeliveryEvidence(deliveryId, evidence) {
      if (!ID.test(deliveryId || '') || !REQUEST_KEY.test(evidence.provider_email_id || '') || !['processed','delivered','rejected','soft-bounced','hard-bounced'].includes(evidence.event_type) || typeof evidence.event_at !== 'string' || evidence.event_at.length > 40) throw new ConsoleError('Invalid provider evidence.');
      const data = await request(`/reviews-admin/v1/deliveries/${deliveryId}/acknowledge`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider_email_id: evidence.provider_email_id, event_type: evidence.event_type, event_at: evidence.event_at }) }, true);
      if (data?.status !== 'acknowledged') throw new ConsoleError('Provider evidence could not be acknowledged.');
    },
    async deliveryEvidence(deliveryId) {
      if (!ID.test(deliveryId || '')) throw new ConsoleError('Invalid delivery reference.');
      const data = await request(`/reviews-admin/v1/deliveries/${deliveryId}/provider-evidence`, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      const validEvent = (row) => row && ['processed', 'delivered', 'rejected', 'soft-bounced', 'hard-bounced'].includes(row.event_type) && typeof row.event_at === 'string' && row.event_at.length <= 40 && Number.isSafeInteger(row.observed_at);
      if (!data || data.delivery_identity !== deliveryId || !SUBJECT.test(data.acquisition_id || '') || !ID.test(data.generation_id || '') || !Array.isArray(data.activity) || !Array.isArray(data.discoveries) || data.activity.length > 50 || data.discoveries.length > 50 || data.activity.some((row) => !validEvent(row)) || data.discoveries.some((row) => !validEvent(row) || !REQUEST_KEY.test(row.provider_email_id || '')) || (data.provider !== null && (!data.provider || data.provider.provider !== 'smtp2go' || !REQUEST_KEY.test(data.provider.provider_email_id || '') || !Number.isSafeInteger(data.provider.accepted_at))) || typeof data.truncated !== 'boolean') throw new ConsoleError('The review service returned invalid provider evidence.');
      return {
        provider: data.provider ? { provider: data.provider.provider, provider_email_id: data.provider.provider_email_id, accepted_at: data.provider.accepted_at } : null,
        activity: data.activity.map(({event_type, event_at, observed_at}) => ({event_type, event_at, observed_at})),
        discoveries: data.discoveries.map(({provider_email_id, event_type, event_at, observed_at}) => ({provider_email_id, event_type, event_at, observed_at})),
        audit: (Array.isArray(data.audit) ? data.audit : []).filter((row) => row && typeof row.actor === 'string' && ['refresh_requested', 'evidence_acknowledged'].includes(row.action) && Number.isSafeInteger(row.occurred_at)).map(({actor, action, occurred_at}) => ({actor, action, occurred_at})),
        truncated: data.truncated,
      };
    },
    async setSubjectName(subject, name) {
      if (!SUBJECT.test(subject || '') || typeof name !== 'string' || !name.trim() || [...name.trim()].length > 200) throw new ConsoleError('Enter a valid product reference and a public name of up to 200 characters.', 400);
      const data = await request('/reviews-admin/v1/subject-display-names', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ subject_id: subject, public_name: name.trim() }) }, true);
      if (!data || data.status !== 'saved') throw new ConsoleError(UNCERTAIN);
      return { status: 'saved' };
    },
    async decide(item, action) {
      if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') || !['approve', 'reject', 'withdraw'].includes(action)) throw new ConsoleError('Invalid moderation request. Refresh the queue.');
      const data = await request(`/reviews-admin/v1/contributions/${item.review_id}/${action}`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ revision_id: item.revision_id }) }, true);
      const expected = { approve: 'published', reject: 'rejected', withdraw: 'withdrawn' }[action];
      if (!data || typeof data !== 'object' || data.review_id !== item.review_id || data.status !== expected) throw new ConsoleError(UNCERTAIN);
      return data;
    },
  };
}
