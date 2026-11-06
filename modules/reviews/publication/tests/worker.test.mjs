import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.mjs';

const digest = 'a'.repeat(64), token = 'synthetic-only-0123456789abcdef0123456789abcdef';
function fakeDb(row) {
  const calls = [];
  return { calls, withSession: () => ({ prepare: (sql) => ({ first: async () => { calls.push(sql); return row; } }) }),
    prepare: (sql) => ({ bind: (...args) => ({ sql, args }) }),
    batch: async (statements) => { calls.push(...statements); return statements.map(() => ({ meta: { changes: 1 } })); } };
}
const environment = (db) => ({ PUBLICATION_MODE: 'test', PUBLICATION_ENVIRONMENT: 'test', PUBLICATION_DB: db, PUBLICATION_TEST_TOKEN: token, PUBLICATION_CANONICAL_ORIGIN: 'https://new.example' });
test('disabled and production modes expose no publication capability', async () => {
  for (const env of [{}, { PUBLICATION_MODE: 'test', PUBLICATION_ENVIRONMENT: 'production', PUBLICATION_DB: {} }]) {
    assert.equal((await worker.fetch(new Request('https://publisher.example/publication/html'), env)).status, 404);
  }
});
test('a changed canonical origin quarantines old HTML before any source request', async () => {
  const db = fakeDb({ state: 'active', digest, epoch: 4, canonical_origin: 'https://old.example', projection: '{}', html: 'WITHDRAWN', jsonld: '{}', media: '[]' });
  const response = await worker.fetch(new Request('https://publisher.example/publication/html'), environment(db));
  assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /WITHDRAWN/);
  assert.equal(db.calls.filter((call) => typeof call === 'object').length, 2);
});
test('recovery cannot rebind an artifact to a different canonical origin', async () => {
  const db = fakeDb({ state: 'safe', digest, epoch: 4, canonical_origin: 'https://old.example', projection: '{}', media: '[]' });
  const request = new Request('https://publisher.example/__test/recover', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ approved_sha256: digest, expected_epoch: 4 }) });
  const response = await worker.fetch(request, environment(db));
  assert.equal(response.status, 409); assert.equal(db.calls.filter((call) => typeof call === 'object').length, 0);
});

test('SQLite-backed publication and withdrawal preserve the public boundary', async () => {
  const { DatabaseSync } = await import('node:sqlite');
  const { readFileSync } = await import('node:fs');
  const { canonical, normalizeProjection, publicationArtifact, sha256 } = await import('../projection.mjs');
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
  const prepare = (sql) => {
    const bind = (...args) => ({
      first: async () => sqlite.prepare(sql).get(...args) ?? null,
      run: async () => ({ meta: { changes: Number(sqlite.prepare(sql).run(...args).changes) } }),
    });
    return { bind, first: bind().first };
  };
  const db = { prepare, withSession: () => ({ prepare }), batch: async (statements) => {
    sqlite.exec('BEGIN');
    try { const result = []; for (const statement of statements) result.push(await statement.run()); sqlite.exec('COMMIT'); return result; }
    catch (error) { sqlite.exec('ROLLBACK'); throw error; }
  } };
  const input = JSON.parse(readFileSync(new URL('./fixtures/r4-projection.json', import.meta.url), 'utf8'));
  const source = 'https://source.example', origin = 'https://new.example';
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const path = new URL(url).pathname;
    let data;
    if (path.endsWith('/subject-lookup')) data = { subject_id: input.subject.id, kind: input.subject.kind, canonical_path: input.subject.canonical_path };
    else if (path.endsWith('/summary')) data = input.summary;
    else if (path.endsWith('/contributions')) { data = { ...input.pages[0] }; delete data.request_after; }
    else return new Response(null, { status: 404 });
    return Response.json(data, { headers: { 'Cache-Control': 'no-store' } });
  };
  try {
    const env = { ...environment(db), REVIEWS_PUBLIC_ORIGIN: source, REVIEWS_SOURCE_SCOPE: 'example-store:test' };
    const projection = normalizeProjection(input);
    const approved = await sha256(canonical(publicationArtifact(projection, [], origin, source, 'example-store:test')));
    const post = (path, body, auth = true) => worker.fetch(new Request(`https://publisher.example${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(auth ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body) }), env);
    assert.equal((await post('/__test/publish', {}, false)).status, 401);
    assert.equal((await post('/__test/publish', { projection: input, media: [], approved_sha256: approved })).status, 200);
    assert.equal((await post('/__test/publish', { projection: input, media: [], approved_sha256: approved })).status, 200);
    let response = await worker.fetch(new Request('https://publisher.example/publication/html'), env);
    assert.equal(response.status, 200); assert.match(await response.text(), /Good/);
    response = await worker.fetch(new Request('https://publisher.example/publication/jsonld'), env);
    assert.equal((await response.json()).aggregateRating.ratingCount, 2);
    const state = await (await post('/__test/state', {})).json();
    assert.equal((await post('/__test/withdraw', { expected_digest: approved, expected_epoch: state.epoch, record_id: 'r' })).status, 200);
    response = await worker.fetch(new Request('https://publisher.example/publication/html'), env);
    assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /Good/);
    assert.equal((await post('/__test/publish', { projection: input, media: [], approved_sha256: approved })).status, 409);
  } finally { globalThis.fetch = originalFetch; sqlite.close(); }
});
