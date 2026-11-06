import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeProjection, canonical, sha256, renderPublication, publicationArtifact } from '../projection.mjs';

function input() {
  const subject = { id: 'policy', kind: 'product', name: 'Example <policy>', canonical_path: '/product/policy/' };
  const summary = { subject_id: 'policy', snapshot_id: 'publication-v1', rating: { average: 4.5, rated_count: 2 }, contribution_count: 3 };
  const contributions = [
    { id: 'h-1', source: { kind: 'historical', attribution: 'Legacy provider' }, author: 'A', title: 'Helpful', body: '<script>bad()</script>', rating: 5, date: { original: '2023-01-02', precision: 'date', timezone_provenance: 'unknown', utc: null, private_email: 'canary@example.invalid' }, verification: 'legacy_woo_verified_assertion', anchors: ['comment-1'], replies: [{ id: 'h-reply', source: { kind: 'historical' }, author: 'Staff', body: 'Thanks', rating: null, replies: [] }] },
    { id: 'n-' + 'a'.repeat(64), source_id: 'a'.repeat(64), source: { kind: 'native' }, author: 'B', body: 'Good', rating: 4, created_at: 1700000000, verification: 'cartray_verified_acquisition', replies: [] },
    { id: 'n-' + 'b'.repeat(64), source_id: 'b'.repeat(64), source: { kind: 'native' }, author: 'C', body: 'A note', rating: null, replies: [] }
  ];
  return { subject, summary, pages: [{ subject_id: 'policy', snapshot_id: 'publication-v1', request_after: null, contributions, has_more: false, next_after: null }] };
}
test('one normalized projection produces matching HTML and product JSON-LD without private fields', async () => {
  const value = normalizeProjection(input()); const rendered = renderPublication(value, 'https://store.example');
  assert.equal(value.contribution_count, 3); assert.equal(value.rating.rated_count, 2); assert.equal(value.rating.average, 4.5);
  assert.equal(value.reviews[0].date.original, '2023-01-02'); assert.equal(value.reviews[0].date.timezone_provenance, 'unknown');
  assert.doesNotMatch(canonical(value), /canary@example/);
  assert.match(rendered.html, /&lt;script&gt;bad\(\)&lt;\/script&gt;/); assert.match(rendered.html, /id="comment-1"/);
  const jsonld = JSON.parse(rendered.jsonld); assert.equal(jsonld.name, 'Example <policy>'); assert.equal(jsonld.aggregateRating.ratingCount, 2); assert.equal(jsonld.aggregateRating.ratingValue, 4.5);
  assert.match(rendered.html, /4\.5 from 2 ratings · 3 contributions/);
  assert.equal(await sha256(canonical(value)), await sha256(canonical(normalizeProjection(input()))));
});
test('rejects mixed versions, partial pages, duplicate identities and inconsistent aggregates', () => {
  for (const mutate of [
    (x) => { x.pages[0].snapshot_id = 'wrong'; },
    (x) => { x.pages[0].has_more = true; x.pages[0].next_after = 'n-' + 'b'.repeat(64); },
    (x) => { x.pages[0].contributions[1].id = 'h-1'; },
    (x) => { x.summary.rating.rated_count = 3; },
    (x) => { x.pages[0].contributions[0].id = 42; },
    (x) => { x.pages[0].contributions[0].anchors = ['h-1']; },
    (x) => { delete x.pages[0].contributions[1].source_id; },
    (x) => { x.pages[0].contributions[2] = { ...x.pages[0].contributions[1], id: 'a'.repeat(64), source_id: undefined }; }
  ]) { const value = input(); mutate(value); assert.throws(() => normalizeProjection(value)); }
});
test('service subjects produce no product schema and unsafe canonical origins are rejected', () => {
  const value = input(); value.subject.kind = 'service';
  assert.equal(renderPublication(normalizeProjection(value), 'https://store.example').jsonld, '');
  assert.throws(() => renderPublication(normalizeProjection(value), 'https://user:secret@store.example'));
});

test('canonical lineage survives raw and unified identifiers and linked historical records', () => {
  const a = input(); const nativeId = 'a'.repeat(64);
  const mixed = normalizeProjection(a).reviews[1];
  a.pages[0].contributions[1].id = nativeId;
  delete a.pages[0].contributions[1].source_id;
  const raw = normalizeProjection(a).reviews[1];
  assert.deepEqual(raw.lineage, mixed.lineage);
  const linked = input();
  linked.pages[0].contributions[1].historical_provenance = { source_id: 'old-record', date: { original: '2020-01-01', precision: 'date', timezone_provenance: 'unknown', utc: null }, verification: 'legacy_woo_verified_assertion', votes: { up: null, down: null, net: 2 }, anchors: ['legacy-anchor'], source: { kind: 'historical', attribution: 'Previous provider' } };
  const item = normalizeProjection(linked).reviews[1];
  assert.deepEqual(item.lineage, [`native:${nativeId}`, 'historical:old-record']);
  assert.equal(item.date.original, '2020-01-01');
  assert.equal(item.votes.net, 2);
  assert.match(renderPublication(normalizeProjection(linked), 'https://store.example').html, /Previous provider/);
  linked.pages[0].contributions[0].id = 'old-record';
  assert.throws(() => normalizeProjection(linked), /duplicate source identity/);
});


test('approval digest changes if rendered HTML or JSON-LD changes', async () => {
  const artifact = publicationArtifact(normalizeProjection(input()), [], 'https://store.example', 'https://reviews.example', 'example-store:test');
  const digest = await sha256(canonical(artifact));
  assert.notEqual(digest, await sha256(canonical({ ...artifact, html: artifact.html + '<!-- changed -->' })));
  assert.notEqual(digest, await sha256(canonical({ ...artifact, jsonld: artifact.jsonld + ' ' })));
  assert.equal(artifact.schema, 'cartray.reviews.artifact.v1');
});
