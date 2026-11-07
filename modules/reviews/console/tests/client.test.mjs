import test from 'node:test';
import assert from 'node:assert/strict';
import { createClient, ConsoleError } from '../client.mjs';
const id = 'a'.repeat(64), revision = 'b'.repeat(64);
const review = { review_id: id, revision_id: revision, subject_id: 'policy', author: '<script>', rating: 2, title: null, body: 'feedback' };
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
test('queue is same-origin, bounded and preserves display text as data', async () => {
  const calls = []; const client = createClient(async (...args) => { calls.push(args); return response({ pending: [review], next_after: id }); });
  const result = await client.pending();
  assert.equal(result.pending[0].author, '<script>'); assert.equal(result.next_after, id);
  assert.equal(calls[0][0], '/reviews-admin/v1/pending'); assert.equal(calls[0][1].credentials, 'same-origin');
  assert.ok(!('Authorization' in calls[0][1].headers));
  await client.pending(id); assert.equal(calls[1][0], `/reviews-admin/v1/pending?after=${id}`);
});
test('moderation binds both review and revision, and validates confirmation', async () => {
  let request; const client = createClient(async (url, options) => { request = [url, options]; return response({ review_id: id, status: 'published' }); });
  await client.decide(review, 'approve');
  assert.equal(request[0], `/reviews-admin/v1/contributions/${id}/approve`); assert.deepEqual(JSON.parse(request[1].body), { revision_id: revision });
  await assert.rejects(client.decide(review, 'unknown'), ConsoleError);
  await assert.rejects(createClient(async () => response({ review_id: id, status: 'rejected' })).decide(review, 'approve'), /could not confirm/);
  await assert.rejects(createClient(async () => response(null)).decide(review, 'approve'), /could not confirm.*Refresh/);
});
test('published listing, history pagination and withdrawal use bounded same-origin endpoints', async () => {
  const event = 'c'.repeat(64), calls = [];
  const client = createClient(async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith('/published')) return response({ published: [{ ...review, subject_name: 'Example' }], next_after: null });
    if (url.includes('/revisions')) return response({ review_id: id, revisions: [{ revision_id: revision, author: 'A', body: 'B', title: null, rating: 2 }], next_after: null });
    if (url.includes('/audit')) return response({ review_id: id, audit: [{ event_id: event, revision_id: revision, action: 'approve', actor: 'staff:x', occurred_at: 1 }], next_after: event });
    return response({ review_id: id, status: 'withdrawn' });
  });
  assert.equal((await client.published()).published[0].subject_name, 'Example');
  assert.equal((await client.history(id, 'revisions')).records[0].revision_id, revision);
  assert.equal((await client.history(id, 'audit')).next_after, event);
  await client.history(id, 'audit', event); assert.match(calls[3][0], new RegExp(`after=${event}$`));
  await client.decide(review, 'withdraw'); assert.match(calls[4][0], /\/withdraw$/);
  await assert.rejects(client.history(id, 'untrusted'), /Invalid history/);
  await assert.rejects(createClient(async () => response({ review_id: id, audit: [{ event_id: event, action: {}, actor: 'x', occurred_at: 1 }], next_after: null })).history(id, 'audit'), /invalid response/);
});
test('stalled fetches and response bodies abort within the deadline and explain uncertain decisions', async () => {
  let signal;
  const hanging = async (_url, options) => { signal = options.signal; return new Promise(() => {}); };
  await assert.rejects(createClient(hanging, 15).pending(), /timed out/);
  assert.equal(signal.aborted, true);
  await assert.rejects(createClient(hanging, 15).decide(review, 'approve'), /could not confirm.*Refresh/);
  assert.equal(signal.aborted, true);
  const stalledBody = async (_url, options) => { signal = options.signal; return { ok: true, json: () => new Promise(() => {}) }; };
  await assert.rejects(createClient(stalledBody, 15).pending(), /timed out/);
  assert.equal(signal.aborted, true);
  await assert.rejects(createClient(stalledBody, 15).decide(review, 'approve'), /could not confirm.*Refresh/);
  assert.equal(signal.aborted, true);
  await assert.rejects(createClient(async () => response({}, 503), 15).decide(review, 'approve'), /could not confirm.*Refresh/);
});
test('invalid queue records, cursors and unexpected service responses fail closed', async () => {
  await assert.rejects(createClient().pending('bad'), /cursor/);
  for (const value of [{ pending: [{ ...review, review_id: '../x' }], next_after: null }, { pending: [review, review], next_after: null }, { pending: [], next_after: id }]) {
    await assert.rejects(createClient(async () => response(value)).pending(), /invalid response/);
  }
});
test('staff authorization, conflict and rate errors provide actionable messages', async () => {
  for (const [code, text] of [[401, /staff session/], [403, /permission/], [409, /changed/], [429, /wait/], [503, /temporarily/]]) {
    await assert.rejects(createClient(async () => response({}, code)).pending(), text);
  }
});

test('reply requests bind parent and reply revisions and retain caller idempotency key', async () => {
  const replyId = 'c'.repeat(64), replyRevision = 'd'.repeat(64), calls = [];
  const client = createClient(async (url, options) => {
    calls.push([url, options]);
    if (url.includes('reply-revisions')) return response({ review_id: id, state: 'published', current_revision_id: revision, reply: { reply_id: replyId, reply_revision_id: replyRevision, parent_revision_id: revision, state: 'published' }, reply_revisions: [{ reply_revision_id: replyRevision, author: 'Store', body: 'Thanks', created_at: 1 }], next_after: null });
    if (url.includes('reply-audit')) return response({ review_id: id, state: 'published', current_revision_id: revision, reply_audit: [{ event_id: 'e'.repeat(64), reply_id: replyId, reply_revision_id: replyRevision, actor: 'staff:x', action: 'created', occurred_at: 1 }], next_after: null });
    return response({ reply_id: replyId, reply_revision_id: replyRevision, state: url.endsWith('reply-withdraw') ? 'withdrawn' : 'published' });
  });
  assert.equal((await client.replyHistory(review)).head.reply_revision_id, replyRevision);
  await client.reply(review, 'upsert', { expected_reply_revision_id: replyRevision, author: 'Store', body: 'Thanks' }, 'request-1');
  assert.equal(calls[1][1].headers['Idempotency-Key'], 'request-1');
  assert.deepEqual(JSON.parse(calls[1][1].body), { parent_revision_id: revision, expected_reply_revision_id: replyRevision, author: 'Store', body: 'Thanks' });
  await client.reply(review, 'withdraw', { expected_reply_revision_id: replyRevision }, 'request-2');
  assert.match(calls[2][0], /reply-withdraw$/);
  assert.equal((await client.replyAudit(review)).records[0].action, 'created');
  await assert.rejects(client.reply(review, 'upsert', { expected_reply_revision_id: null, author: ' ', body: 'x' }, 'x'), /display name/);
  await assert.rejects(createClient(async () => response({ review_id: id, state: 'published', current_revision_id: 'f'.repeat(64) })).replyHistory(review), /changed/);
});

test('delivery investigation requires explicit state and paginates by delivery identity', async () => {
  const generation = 'c'.repeat(64), delivery = 'd'.repeat(64), calls = [];
  const client = createClient(async (url, options) => {
    calls.push([url, options]);
    return response({ state: 'uncertain', deliveries: [{ generation_id: generation, delivery_identity: delivery, state: 'uncertain', prepared_at: 10, private_email: 'discard' }], next_after: delivery });
  });
  const page = await client.deliveries('uncertain');
  assert.equal(page.records[0].private_email, undefined);
  await client.deliveries('uncertain', page.next_after);
  assert.match(calls[1][0], new RegExp(`after=${delivery}$`));
  await assert.rejects(client.deliveries('accepted'), /filter/);
  await assert.rejects(createClient(async () => response({}, 401)).deliveries('uncertain'), /permission/);
});
