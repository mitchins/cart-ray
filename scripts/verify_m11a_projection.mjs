import { createHash, createPublicKey, verify as verifySignature } from 'node:crypto';
import { readFileSync } from 'node:fs';

const SIGNED_FIELDS = {
  catalogue_version: 'cr_catalogue_version',
  environment: null,
  item_count: 'cr_item_count',
  items_digest: 'cr_items_digest',
  key_id: 'cr_kid',
  nonce: 'cr_nonce',
  order_id: 'cr_order_id',
  schema: 'cr_schema',
  session_id: null,
  source: 'cr_source',
};

function decodeCanonicalBase64url(value, length) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('non-canonical base64url');
  const bytes = Buffer.from(value, 'base64url');
  if (bytes.length !== length || bytes.toString('base64url') !== value) throw new Error('non-canonical base64url');
  return bytes;
}

function asciiSafeJson(value) {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

export function canonicalPayload(sessionId, environment, metadata) {
  const values = {};
  for (const [name, source] of Object.entries(SIGNED_FIELDS)) {
    values[name] = source === null ? (name === 'environment' ? environment : sessionId) : metadata[source];
  }
  return Buffer.from(asciiSafeJson(values), 'utf8');
}

export function verifyProjection({ session_id: sessionId, configured_environment: environment, metadata, trusted_public_keys: keyring }) {
  if (!['test', 'live'].includes(environment) || !/^cs_[A-Za-z0-9_]+$/.test(sessionId)) return null;
  if (!metadata || typeof metadata !== 'object' || metadata.cr_schema !== '1' || metadata.cr_source !== 'cartray') return null;
  const required = ['cr_schema', 'cr_source', 'cr_order_id', 'cr_catalogue_version', 'cr_item_count',
    'cr_chunk_count', 'cr_items_digest', 'cr_nonce', 'cr_kid', 'cr_signature'];
  if (required.some((key) => typeof metadata[key] !== 'string')) return null;
  if (!keyring || typeof keyring !== 'object' || Array.isArray(keyring) || !Object.keys(keyring).length) return null;
  if (!Object.hasOwn(keyring, metadata.cr_kid)) return null;
  const publicKey = keyring[metadata.cr_kid];
  if (!/^[1-9][0-9]*$/.test(metadata.cr_item_count) || !/^[1-9][0-9]*$/.test(metadata.cr_chunk_count)) return null;
  const chunkCount = Number(metadata.cr_chunk_count);
  if (!Number.isSafeInteger(chunkCount) || chunkCount > 32) return null;
  const chunkKeys = Array.from({ length: chunkCount }, (_, i) => `cr_items_${String(i + 1).padStart(2, '0')}`);
  const allowed = new Set([...required, ...chunkKeys]);
  if (Object.keys(metadata).some((key) => key.startsWith('cr_') && !allowed.has(key))) return null;
  if (chunkKeys.some((key) => typeof metadata[key] !== 'string' || Buffer.byteLength(metadata[key]) > 400)) return null;
  const chunks = chunkKeys.map((key) => metadata[key]);
  const tokens = chunks.join(',').split(',');
  if (tokens.length !== Number(metadata.cr_item_count) || tokens.some((token) => !/^[A-Z0-9][A-Z0-9_-]{0,63}:[1-9][0-9]*$/.test(token))) return null;
  const items = tokens.map((token) => {
    const colon = token.lastIndexOf(':');
    return [token.slice(0, colon), token.slice(colon + 1)];
  });
  if (items.some(([key, quantity], i) => (i > 0 && items[i - 1][0] >= key) || !Number.isSafeInteger(Number(quantity)))) return null;
  const digest = createHash('sha256').update(`cartray-items-v1\n${tokens.join('\n')}\n`).digest('hex');
  if (metadata.cr_items_digest !== `sha256:${digest}`) return null;
  const canonicalChunks = [];
  for (const token of tokens) {
    const previous = canonicalChunks.length - 1;
    if (previous < 0 || Buffer.byteLength(`${canonicalChunks[previous]},${token}`) > 400) canonicalChunks.push(token);
    else canonicalChunks[previous] += `,${token}`;
  }
  if (canonicalChunks.length !== chunks.length || canonicalChunks.some((chunk, i) => chunk !== chunks[i])) return null;
  try {
    const signature = decodeCanonicalBase64url(metadata.cr_signature, 64);
    const rawKey = decodeCanonicalBase64url(publicKey, 32);
    const spki = Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), rawKey]);
    if (!verifySignature(null, canonicalPayload(sessionId, environment, metadata),
      createPublicKey({ key: spki, format: 'der', type: 'spki' }), signature)) return null;
    return items.map(([key, quantity]) => [key, Number(quantity)]);
  } catch {
    return null;
  }
}

if (process.argv[1]?.endsWith('/verify_m11a_projection.mjs')) {
  try {
    const input = process.argv[2] ? JSON.parse(readFileSync(process.argv[2], 'utf8')) : JSON.parse(readFileSync(0, 'utf8'));
    const items = verifyProjection(input);
    if (items === null) process.exitCode = 1;
    else process.stdout.write(`${JSON.stringify({ items })}\n`);
  } catch {
    process.exitCode = 1;
  }
}
