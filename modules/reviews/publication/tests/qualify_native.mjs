// Greenfield synthetic native-only publication, exercised against actual local Workerd/D1.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { canonical, normalizeProjection, publicationArtifact, sha256 } from '../projection.mjs';
const [gate, source, proxy, fixture] = process.argv.slice(2);
for (const url of [gate, source, proxy]) if (new URL(url).hostname !== '127.0.0.1') throw new Error('local test only');
const input = JSON.parse(fs.readFileSync(fixture, 'utf8'));
const projection = normalizeProjection(input);
assert.match(projection.source_snapshot, /^native-\d+$/);
const digest = await sha256(canonical(publicationArtifact(projection, [], 'https://example.invalid', proxy, 'example-store:test')));
const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.PUBLICATION_TEST_TOKEN}` };
const publish = await fetch(new URL('/__test/publish', gate), { method: 'POST', headers, body: JSON.stringify({ projection: input, media: [], approved_sha256: digest }) });
assert.equal(publish.status, 200, await publish.clone().text());
const counts = async () => (await fetch(new URL('/__test/counts', proxy))).json();
const before = await counts();
const start = performance.now();
for (const path of ['/publication/html', '/publication/jsonld']) {
  const response = await fetch(new URL(path, gate)); assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-publication-digest'), digest);
}
const elapsed = Math.round(performance.now() - start);
const after = await counts();
assert.equal(after['/reviews/v1/subjects/example-policy/contributions'], before['/reviews/v1/subjects/example-policy/contributions']);
assert.equal(after['/reviews/v1/subject-lookup'] - before['/reviews/v1/subject-lookup'], 2);
assert.equal(after['/reviews/v1/subjects/example-policy/summary'] - before['/reviews/v1/subjects/example-policy/summary'], 2);
const review = input.pages[0].contributions[0];
const withdrawn = await fetch(new URL(`/reviews-admin/v1/contributions/${review.id}/withdraw`, source), {
  method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.REVIEWS_TEST_ADMIN_TOKEN}` },
  body: JSON.stringify({ revision_id: process.env.REVIEWS_TEST_REVISION }),
});
assert.equal(withdrawn.status, 200, await withdrawn.clone().text());
for (const path of ['/publication/html', '/publication/jsonld']) {
  const response = await fetch(new URL(path, gate)); assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /Synthetic greenfield review/);
}
console.log(JSON.stringify({ source: 'native synthetic Workerd/D1', full_pages_per_public_read: 0, revision_requests_per_public_read: 2, local_two_reads_ms: elapsed, withdrawal_fallback: true }));
