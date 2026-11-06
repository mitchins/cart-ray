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
