// Synthetic-only, destructive to its disposable publication DB. Never use a production URL.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import { canonical, normalizeProjection, publicationArtifact, sha256 } from '../projection.mjs';

const [baseArg, projectionPath] = process.argv.slice(2);
const base = new URL(baseArg), token = process.env.PUBLICATION_TEST_TOKEN;
const sourceOrigin = process.env.REVIEWS_PUBLIC_ORIGIN;
if (!sourceOrigin) throw new Error('synthetic source origin required');
if (!token || token.length < 32 || !projectionPath || !((base.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(base.hostname)) || (base.protocol === 'https:' && base.hostname.startsWith('r5-') && base.hostname.endsWith('.workers.dev')))) throw new Error('disposable synthetic test target and token required');
const input = JSON.parse(fs.readFileSync(projectionPath, 'utf8'));
const projection = normalizeProjection(input);
if (projection.subject.id !== 'example-policy' || projection.contribution_count !== 3) throw new Error('unexpected synthetic fixture');
const data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const mediaSha = await sha256(Uint8Array.from(Buffer.from(data, 'base64')));
const root = projection.reviews.find((item) => item.id === 'r'); if (!root) throw new Error('fixture root absent');
const media = [{ sha256: mediaSha, record_id: root.id, lineage: root.lineage, type: 'image/png', data }];
const artifact = publicationArtifact(projection, media, 'https://example.invalid', new URL(sourceOrigin).origin, 'example-store:test');
const approved = await sha256(canonical(artifact));
const payload = { projection: input, media, approved_sha256: approved };
const post = (path, body, authorized = true) => fetch(new URL(path, base), { method: 'POST', headers: { 'Content-Type': 'application/json', ...(authorized ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body), cache: 'no-store' });
assert.equal((await post('/__test/publish', payload, false)).status, 401);
const published = await post('/__test/publish', payload);
if (published.status !== 200) {
  const diagnostic = await post('/__test/source-diagnostic', {});
  throw new Error(`publication ${published.status}: ${await published.text()}; source diagnostic ${diagnostic.status}: ${await diagnostic.text()}`);
}
assert.equal((await published.json()).digest, approved);
assert.equal((await post('/__test/publish', payload)).status, 200, 'repeat publication should be idempotent');
const changedRendererDigest = await sha256(canonical({ ...artifact, html: artifact.html + '<!-- renderer changed -->' }));
assert.notEqual(changedRendererDigest, approved);
assert.equal((await post('/__test/publish', { ...payload, approved_sha256: changedRendererDigest })).status, 409, 'changed rendered bytes cannot reuse approval');
const paths = ['/publication/html', '/publication/jsonld', `/publication/media/${mediaSha}`];
for (const path of paths) {
  const response = await fetch(new URL(path, base), { cache: 'no-store' });
  assert.equal(response.status, 200, path); assert.match(response.headers.get('cache-control'), /no-store/);
  assert.equal(response.headers.get('x-publication-digest'), approved);
  if (path.endsWith('/html')) { const html = await response.text(); assert.equal(html, artifact.html); assert.match(html, /Good/); assert.match(html, /application\/ld\+json/); }
  if (path.endsWith('/jsonld')) { const json = await response.json(); assert.equal(json.aggregateRating.ratingCount, 2); assert.equal(json.aggregateRating.ratingValue, 4.5); }
}
// Control is either the local R4 proxy or the independently deployed synthetic source.
const control = process.env.REVIEWS_SOURCE_CONTROL;
if (control) {
  const controlUrl = new URL(control);
  const local = controlUrl.protocol === 'http:' && controlUrl.hostname === '127.0.0.1';
  const hosted = controlUrl.protocol === 'https:' && /^r5-src-[a-f0-9]+\.[a-z0-9-]+\.workers\.dev$/.test(controlUrl.hostname);
  if (!local && !hosted) throw new Error('disposable source control only');
  const controlPost = (path) => fetch(new URL(path, controlUrl), { method: 'POST', headers: hosted ? { Authorization: `Bearer ${token}` } : {} });
  assert.equal((await controlPost('/__test/off')).status, 200);
  assert.equal((await fetch(new URL('/publication/html', base))).status, 503);
  assert.equal((await controlPost('/__test/on')).status, 200);
  assert.equal((await fetch(new URL('/publication/html', base))).status, 503, 'outage quarantine must be sticky');
  assert.equal((await post('/__test/publish', payload)).status, 409, 'retired artifact must not silently reactivate');
  const state = await (await post('/__test/state', {})).json();
  const recovery = await post('/__test/recover', { approved_sha256: approved, expected_epoch: state.epoch });
  assert.equal(recovery.status, 200, await recovery.clone().text());
  assert.equal((await fetch(new URL('/publication/html', base))).status, 200);
}
// Remove media in a later publication before withdrawal. Its historical ownership
// must still be tombstoned even though it is absent from the active artifact.
const laterDigest = await sha256(canonical(publicationArtifact(projection, [], 'https://example.invalid', new URL(sourceOrigin).origin, 'example-store:test')));
const later = await post('/__test/publish', { projection: input, media: [], approved_sha256: laterDigest });
assert.equal(later.status, 200, await later.clone().text());
if (control) {
  const headers = new URL(control).protocol === 'https:' ? { Authorization: `Bearer ${token}` } : {};
  assert.equal((await fetch(new URL('/__test/off', control), { method: 'POST', headers })).status, 200);
  assert.equal((await fetch(new URL('/publication/html', base))).status, 503);
  assert.equal((await fetch(new URL('/__test/on', control), { method: 'POST', headers })).status, 200);
}
const beforeWithdraw = await (await post('/__test/state', {})).json();
if (control) assert.equal(beforeWithdraw.state, 'safe');
const start = performance.now();
const withdrawn = await post('/__test/withdraw', { expected_digest: laterDigest, expected_epoch: beforeWithdraw.epoch, record_id: root.id });
assert.equal(withdrawn.status, 200, await withdrawn.clone().text());
const ack = performance.now();
const checks = await Promise.all(Array.from({ length: 5 }, async (_, iteration) => Promise.all(paths.map(async (path) => {
  const url = new URL(path, base); url.searchParams.set('probe', String(iteration));
  const response = await fetch(url, { cache: 'no-store' }); const text = await response.text();
  assert.equal(response.status, 503, path); assert.doesNotMatch(text, /Good|application\/ld\+json/); assert.match(response.headers.get('cache-control'), /no-store/);
}))));
assert.equal(checks.length, 5);
const replay = await post('/__test/publish', payload); assert.equal(replay.status, 409, 'withdrawn content must not be republished');
const safeState = await (await post('/__test/state', {})).json();
const deniedRecovery = await post('/__test/recover', { approved_sha256: laterDigest, expected_epoch: safeState.epoch });
assert.equal(deniedRecovery.status, 409, 'withdrawn content must not be recovered');
assert.match(await deniedRecovery.text(), /content withdrawn/, 'recovery must fail on tombstones, not source availability');
console.log(JSON.stringify({ synthetic: true, approved_digest: approved, withdrawn_digest: laterDigest, media_digest: mediaSha, rated_count: 2, contribution_count: 3, withdraw_ack_ms: Math.round(ack - start), post_withdraw_15_reads_ms: Math.round(performance.now() - ack), old_publication_rejected: true, outage_recovery_tested: Boolean(control) }));
