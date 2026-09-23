import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { canonicalPayload, verifyProjection } from '../scripts/verify_m11a_projection.mjs';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/m11a_schema1_projection.json', import.meta.url), 'utf8'));
const trustedFixture = { ...fixture, trusted_public_keys: { 'kid-fixture-1': fixture.ed25519_public_key_base64url } };
const expected = '{"catalogue_version":"sha256:8d3674ae409653d9194f71fcdeb5f7b24f4d8442aac3fd966d0c2612aeba71ee","environment":"test","item_count":"2","items_digest":"sha256:bc015bfe893b50b78899b3dd40de8734c7b43ee9c8e67fcc419507b792a24e75","key_id":"kid-fixture-1","nonce":"nonce-fixture","order_id":"cr_order_fixture","schema":"1","session_id":"cs_test_fixture","source":"cartray"}';

test('frozen M11a fixture verifies with independent Node Ed25519', () => {
  assert.equal(canonicalPayload(fixture.session_id, fixture.configured_environment, fixture.metadata).toString(), expected);
  assert.deepEqual(verifyProjection(trustedFixture), fixture.expected_items);
});

test('every signed field, chunk, session and environment mutation fails', () => {
  for (const field of ['cr_schema', 'cr_source', 'cr_order_id', 'cr_catalogue_version', 'cr_item_count',
    'cr_items_digest', 'cr_nonce', 'cr_kid', 'cr_chunk_count', 'cr_items_01', 'cr_signature']) {
    assert.equal(verifyProjection({ ...trustedFixture, metadata: { ...fixture.metadata, [field]: `${fixture.metadata[field]}x` } }), null, field);
  }
  for (const environment of ['live', 'TEST', ' test ', 'staging', null]) {
    assert.equal(verifyProjection({ ...trustedFixture, configured_environment: environment }), null, String(environment));
  }
  assert.equal(verifyProjection({ ...trustedFixture, session_id: 'cs_test_other' }), null);
  assert.equal(verifyProjection({ ...trustedFixture, metadata: { ...fixture.metadata, cr_unknown: 'x' } }), null);
  assert.equal(verifyProjection({ ...trustedFixture, metadata: { ...fixture.metadata, cr_items_02: 'EP-SIL-2026:1' } }), null);
  assert.equal(verifyProjection({ ...trustedFixture, metadata: { ...fixture.metadata, cr_signature: `${fixture.metadata.cr_signature}==` } }), null);
  assert.equal(verifyProjection({ ...trustedFixture, metadata: { ...fixture.metadata, cr_signature: 'A'.repeat(86) } }), null);
});

test('unknown or retired key fails with non-empty replacement keyring', () => {
  const replacement = { 'kid-replacement': fixture.ed25519_public_key_base64url };
  assert.deepEqual(verifyProjection(trustedFixture), fixture.expected_items);
  assert.equal(verifyProjection({ ...trustedFixture, trusted_public_keys: replacement }), null);
  assert.equal(verifyProjection({ ...trustedFixture, metadata: { ...fixture.metadata, cr_kid: 'kid-unknown' } }), null);
});
