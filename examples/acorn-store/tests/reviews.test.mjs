import assert from 'node:assert/strict';
import test from 'node:test';
import { reviewsPage } from '../reviews.mjs';

const summary = { subject_id: 'acorn-growing-kit', snapshot_id: 'native-1', rating: { average: 4, rated_count: 1 }, contribution_count: 2 };
const page = { subject_id: 'acorn-growing-kit', snapshot_id: 'native-1', contributions: [], next_after: null, has_more: false };
function fakeFetch(requests, values = [summary, page]) {
  return async (url, options) => {
    requests.push({ url, options });
    return Response.json(url.includes('/summary') ? values[0] : values[1]);
  };
}
test('loads same-subject matching public revisions without credentials', async () => {
  const requests = [];
  const result = await reviewsPage('acorn-growing-kit', null, null, fakeFetch(requests));
  assert.equal(result.summary.rating.average, 4);
  assert.equal(result.summary.contribution_count, 2);
  assert.equal(requests.length, 2);
  assert.ok(requests.every(({ options }) => options.credentials === 'omit' && options.cache === 'no-store'));
});
test('rejects mismatched revisions and subjects instead of displaying inconsistent data', async () => {
  await assert.rejects(reviewsPage('acorn-growing-kit', null, null, fakeFetch([], [summary, { ...page, snapshot_id: 'native-2' }])));
  await assert.rejects(reviewsPage('acorn-growing-kit', null, null, fakeFetch([], [summary, { ...page, subject_id: 'other' }])));
});
test('pagination binds its cursor to the original revision', async () => {
  const requests = [];
  await reviewsPage('acorn-growing-kit', 'abc123', 'native-1', fakeFetch(requests));
  assert.ok(requests.some(({ url }) => url.includes('after=abc123') && url.includes('snapshot=native-1')));
  await assert.rejects(reviewsPage('acorn-growing-kit', 'abc123', 'native-0', fakeFetch([])));
});
test('rejects malformed subjects, cursors and unavailable service', async () => {
  await assert.rejects(reviewsPage('../admin'));
  await assert.rejects(reviewsPage('acorn-growing-kit', '../bad', 'native-1'));
  await assert.rejects(reviewsPage('acorn-growing-kit', null, null, async () => new Response('down', { status: 503 })));
});
