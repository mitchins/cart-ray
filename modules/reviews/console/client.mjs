// Same-origin only: the trusted access proxy supplies the independently verified assertion.
const ID = /^[a-f0-9]{64}$/;
export class ConsoleError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}
function entry(item) {
  if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') ||
      typeof item.subject_id !== 'string' || item.subject_id.length > 128 ||
      ![null, undefined].includes(item.rating) && (!Number.isInteger(item.rating) || item.rating < 1 || item.rating > 5) ||
      ['author', 'title', 'body'].some((key) => item[key] != null && typeof item[key] !== 'string')) {
    throw new ConsoleError('The review service returned an invalid response.');
  }
  return { review_id: item.review_id, revision_id: item.revision_id, subject_id: item.subject_id,
    author: item.author || '', title: item.title || '', body: item.body || '', rating: item.rating ?? null };
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
  return {
    async pending(after = null) {
      if (after !== null && !ID.test(after)) throw new ConsoleError('Invalid queue cursor. Refresh the queue.');
      const data = await request('/reviews-admin/v1/pending' + (after ? `?after=${after}` : ''), { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (!data || !Array.isArray(data.pending) || data.pending.length > 50 || !(data.next_after === null || ID.test(data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
      const pending = data.pending.map(entry);
      if (new Set(pending.map((item) => item.review_id)).size !== pending.length || (data.next_after && !pending.some((item) => item.review_id === data.next_after))) throw new ConsoleError('The review service returned an invalid response.');
      return { pending, next_after: data.next_after };
    },
    async decide(item, action) {
      if (!item || !ID.test(item.review_id || '') || !ID.test(item.revision_id || '') || !['approve', 'reject'].includes(action)) throw new ConsoleError('Invalid moderation request. Refresh the queue.');
      const data = await request(`/reviews-admin/v1/contributions/${item.review_id}/${action}`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ revision_id: item.revision_id }) }, true);
      if (data.review_id !== item.review_id || data.status !== (action === 'approve' ? 'published' : 'rejected')) throw new ConsoleError(UNCERTAIN);
      return data;
    },
  };
}
