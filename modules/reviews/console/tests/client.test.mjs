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
  await assert.rejects(client.decide(review, 'withdraw'), ConsoleError);
  await assert.rejects(createClient(async () => response({ review_id: id, status: 'rejected' })).decide(review, 'approve'), /could not confirm/);
  await assert.rejects(createClient(async () => response(null)).decide(review, 'approve'), /could not confirm.*Refresh/);
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
