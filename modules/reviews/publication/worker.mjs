import { canonical, normalizeProjection, publicationArtifact, sha256 } from './projection.mjs';

const MAX_BODY = 1_000_000;
const HEX = /^[a-f0-9]{64}$/;
const noStore = { 'Cache-Control': 'no-store, max-age=0', 'CDN-Cache-Control': 'no-store', 'Cloudflare-CDN-Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };
const fallback = '<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow"></head><body><main><h1>Reviews are temporarily unavailable</h1></main></body></html>';
const fail = (status, body = '') => new Response(body, { status, headers: noStore });
function authorized(request, env) {
  const expected = env.PUBLICATION_TEST_TOKEN;
  if (typeof expected !== 'string' || expected.length < 32) return false;
  const actual = request.headers.get('Authorization') || '';
  const wanted = `Bearer ${expected}`;
  if (actual.length !== wanted.length) return false;
  let difference = 0;
  for (let i = 0; i < wanted.length; i++) difference |= wanted.charCodeAt(i) ^ actual.charCodeAt(i);
  return difference === 0;
}
async function bodyJson(request) {
  if (Number(request.headers.get('Content-Length') || 0) > MAX_BODY) throw new Error('body too large');
  // This endpoint is explicitly test-only. Bound streams even without Content-Length.
  const reader = request.body?.getReader(); if (!reader) throw new Error('missing body');
  const chunks = []; let size = 0, timedOut = false;
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => { timedOut = true; reject(new Error('body timeout')); }, 5000); });
  try {
    while (true) {
      const part = await Promise.race([reader.read(), timeout]); if (part.done) break;
      size += part.value.byteLength; if (size > MAX_BODY) throw new Error('body too large');
      chunks.push(part.value);
    }
  } catch (error) {
    // Cancel the sole reader, which also settles a pending read on timeout.
    await reader.cancel(timedOut ? 'timeout' : 'invalid body');
    throw error;
  } finally { clearTimeout(timer); reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}
function flattenLineage(items) { return items.flatMap((item) => [...item.lineage, ...flattenLineage(item.replies)]); }
async function normalizeMedia(media, reviews) {
  if (!Array.isArray(media) || media.length > 10) throw new Error('invalid media');
  const output = [];
  const records = new Map();
  function collect(items) { for (const item of items) { records.set(item.id, item.lineage); collect(item.replies); } }
  collect(reviews);
  for (const item of media) {
    if (!item || typeof item.record_id !== 'string' || !records.has(item.record_id) || !['image/png', 'image/jpeg', 'image/webp'].includes(item.type) || typeof item.data !== 'string' || item.data.length > 140_000 || !/^[A-Za-z0-9+/]*={0,2}$/.test(item.data)) throw new Error('invalid media');
    const binary = Uint8Array.from(atob(item.data), (c) => c.charCodeAt(0));
    if (!binary.length || binary.length > 100_000) throw new Error('invalid media');
    const png = item.type === 'image/png' && [137,80,78,71,13,10,26,10].every((byte, index) => binary[index] === byte);
    const jpeg = item.type === 'image/jpeg' && binary[0] === 255 && binary[1] === 216 && binary[2] === 255;
    const webp = item.type === 'image/webp' && String.fromCharCode(...binary.slice(0, 4)) === 'RIFF' && String.fromCharCode(...binary.slice(8, 12)) === 'WEBP';
    if (!png && !jpeg && !webp) throw new Error('invalid media signature');
    const digest = await sha256(binary);
    if (item.sha256 !== digest || output.some((entry) => entry.sha256 === digest)) throw new Error('media digest mismatch');
    output.push({ sha256: digest, record_id: item.record_id, lineage: records.get(item.record_id), type: item.type, data: item.data });
  }
  return output.sort((a, b) => a.sha256.localeCompare(b.sha256));
}
async function publish(db, payload, origin, env) {
  const projection = normalizeProjection(payload.projection);
  const lineage = flattenLineage(projection.reviews);
  const media = await normalizeMedia(payload.media || [], projection.reviews);
  const source = sourceOrigin(env);
  if (!source || env.REVIEWS_SOURCE_SCOPE !== 'example-store:test') return fail(400, 'invalid synthetic source');
  const artifact = publicationArtifact(projection, media, origin, source.origin, env.REVIEWS_SOURCE_SCOPE);
  const digest = await sha256(canonical(artifact));
  if (!HEX.test(payload.approved_sha256 || '') || payload.approved_sha256 !== digest) return fail(409, 'approval digest mismatch');
  const sourceProjection = await captureSource(env, projection);
  if (!sourceProjection || canonical(sourceProjection) !== canonical(projection)) return fail(409, 'source projection mismatch');
  const { html, jsonld } = artifact;
  const ids = JSON.stringify(lineage);
  const mediaIds = JSON.stringify(media.map((item) => item.sha256));
  const statements = [
    db.prepare('INSERT OR IGNORE INTO publication_artifacts(digest,subject_id,source_origin,source_scope,canonical_origin,projection,html,jsonld,media) VALUES(?,?,?,?,?,?,?,?,?)')
      .bind(digest, projection.subject.id, source.origin, env.REVIEWS_SOURCE_SCOPE, origin, canonical(projection), html, jsonld, canonical(media)),
    db.prepare("INSERT INTO publication_gate(singleton,digest,state,epoch) SELECT 1,?,'active',1 WHERE NOT EXISTS (SELECT 1 FROM publication_retired WHERE digest=?) AND NOT EXISTS (SELECT 1 FROM json_each(?) i JOIN publication_tombstones t ON t.record_id=i.value) AND NOT EXISTS (SELECT 1 FROM json_each(?) i JOIN publication_media_tombstones t ON t.media_sha256=i.value) ON CONFLICT(singleton) DO UPDATE SET digest=excluded.digest,state='active',epoch=publication_gate.epoch+1 WHERE NOT EXISTS (SELECT 1 FROM publication_retired WHERE digest=excluded.digest) AND NOT EXISTS (SELECT 1 FROM json_each(?) i JOIN publication_tombstones t ON t.record_id=i.value) AND NOT EXISTS (SELECT 1 FROM json_each(?) i JOIN publication_media_tombstones t ON t.media_sha256=i.value) AND (publication_gate.state != 'active' OR publication_gate.digest != excluded.digest)")
      .bind(digest, digest, ids, mediaIds, ids, mediaIds)
  ];
  const result = await db.batch(statements);
  if (!result[1].meta.changes) {
    const current = await db.withSession('first-primary').prepare("SELECT digest,state FROM publication_gate WHERE singleton=1").first();
    if (!current || current.digest !== digest || current.state !== 'active') return fail(409, 'publication contains withdrawn content');
  }
  return Response.json({ digest, source_snapshot: projection.source_snapshot }, { headers: noStore });
}
async function withdraw(db, expectedDigest, recordId, expectedEpoch) {
  if (!HEX.test(expectedDigest || '') || typeof recordId !== 'string' || !Number.isSafeInteger(expectedEpoch)) return fail(400);
  const row = await db.withSession('first-primary').prepare("SELECT g.digest,g.state,g.epoch,a.projection,a.media FROM publication_gate g JOIN publication_artifacts a ON a.digest=g.digest WHERE g.singleton=1").first();
  if (!row || row.digest !== expectedDigest || row.epoch !== expectedEpoch) return fail(409, 'publication changed');
  const projection = JSON.parse(row.projection);
  const root = projection.reviews.find((item) => item.id === recordId);
  if (!root) return fail(404);
  const lineage = flattenLineage([root]);
  const statements = [
    db.prepare("INSERT OR IGNORE INTO publication_retired(digest) SELECT ? WHERE EXISTS (SELECT 1 FROM publication_gate WHERE singleton=1 AND digest=? AND epoch=?)").bind(expectedDigest, expectedDigest, expectedEpoch),
    db.prepare("INSERT OR IGNORE INTO publication_tombstones(record_id) SELECT j.value FROM json_each(?) j WHERE EXISTS (SELECT 1 FROM publication_gate WHERE singleton=1 AND digest=? AND epoch=?)").bind(JSON.stringify(lineage), expectedDigest, expectedEpoch),
    db.prepare("INSERT OR IGNORE INTO publication_media_tombstones(media_sha256) SELECT DISTINCT json_extract(m.value,'$.sha256') FROM publication_artifacts a, json_each(a.media) m, json_each(json_extract(m.value,'$.lineage')) l JOIN json_each(?) targets ON targets.value=l.value WHERE EXISTS (SELECT 1 FROM publication_gate WHERE singleton=1 AND digest=? AND epoch=?)").bind(JSON.stringify(lineage), expectedDigest, expectedEpoch)
  ];
  statements.push(db.prepare("UPDATE publication_gate SET state='safe',epoch=epoch+1 WHERE singleton=1 AND digest=? AND epoch=?").bind(expectedDigest, expectedEpoch));
  const result = await db.batch(statements);
  return result.at(-1).meta.changes ? Response.json({ state: 'safe' }, { headers: noStore }) : fail(409, 'publication changed');
}
async function recover(db, payload, env) {
  if (!HEX.test(payload.approved_sha256 || '') || !Number.isSafeInteger(payload.expected_epoch)) return fail(400);
  const row = await db.withSession('first-primary').prepare("SELECT g.digest,g.state,g.epoch,a.projection,a.media,a.source_origin,a.source_scope,a.canonical_origin FROM publication_gate g JOIN publication_artifacts a ON a.digest=g.digest WHERE g.singleton=1").first();
  if (!row || row.state !== 'safe' || row.digest !== payload.approved_sha256 || row.epoch !== payload.expected_epoch) return fail(409, 'safe state changed');
  const projection = JSON.parse(row.projection), media = JSON.parse(row.media);
  if (env.PUBLICATION_CANONICAL_ORIGIN !== row.canonical_origin || !await sourceCurrent(env, projection, row.source_origin, row.source_scope)) return fail(409, 'source projection mismatch');
  const result = await db.prepare("UPDATE publication_gate SET state='active',epoch=epoch+1 WHERE singleton=1 AND digest=? AND epoch=? AND state='safe' AND NOT EXISTS (SELECT 1 FROM json_each(?) i JOIN publication_tombstones t ON t.record_id=i.value) AND NOT EXISTS (SELECT 1 FROM json_each(?) i JOIN publication_media_tombstones t ON t.media_sha256=i.value)").bind(row.digest, row.epoch, JSON.stringify(flattenLineage(projection.reviews)), JSON.stringify(media.map((item) => item.sha256))).run();
  return result.meta.changes ? Response.json({ state: 'active', digest: row.digest }, { headers: noStore }) : fail(409, 'safe state changed or content withdrawn');
}
async function quarantine(db, row) {
  await db.batch([
    db.prepare("INSERT OR IGNORE INTO publication_retired(digest) SELECT ? WHERE EXISTS (SELECT 1 FROM publication_gate WHERE singleton=1 AND digest=? AND epoch=? AND state='active')").bind(row.digest, row.digest, row.epoch),
    db.prepare("UPDATE publication_gate SET state='safe',epoch=epoch+1 WHERE singleton=1 AND digest=? AND epoch=? AND state='active'").bind(row.digest, row.epoch)
  ]);
}
function sourceOrigin(env) {
  let origin;
  try { origin = new URL(env.REVIEWS_PUBLIC_ORIGIN); } catch { return null; }
  const loopback = env.PUBLICATION_ALLOW_LOOPBACK === 'test' && origin.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname);
  if ((!loopback && origin.protocol !== 'https:') || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) return null;
  return origin;
}
async function sourceJson(env, path, signal) {
  const origin = sourceOrigin(env); if (!origin) throw new Error('invalid source');
  const response = await fetch(new URL(path, origin), { headers: { Accept: 'application/json' }, redirect: 'manual', cache: 'no-store', signal });
  if (!response.ok || !/\bno-store\b/i.test(response.headers.get('Cache-Control') || '')) throw new Error('source unavailable');
  const reader = response.body?.getReader(); if (!reader) throw new Error('missing source body');
  const chunks = []; let size = 0;
  try {
    while (true) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.byteLength; if (size > 140000) throw new Error('source response too large');
      chunks.push(part.value);
    }
  } catch (error) {
    try { await reader.cancel('invalid source body'); } catch { /* already aborted */ }
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}
async function sourceCurrent(env, projection, expectedOrigin, expectedScope) {
  if (sourceOrigin(env)?.origin !== expectedOrigin || env.REVIEWS_SOURCE_SCOPE !== expectedScope) return false;
  const current = await captureSource(env, projection);
  return current !== null && canonical(current) === canonical(projection);
}
async function captureSource(env, approved) {
  try {
    const id = encodeURIComponent(approved.subject.id), signal = AbortSignal.timeout(10000);
    const lookup = await sourceJson(env, `/reviews/v1/subject-lookup?alias=${id}`, signal);
    if (lookup.subject_id !== approved.subject.id || lookup.kind !== approved.subject.kind || lookup.canonical_path !== approved.subject.canonical_path) return null;
    const summary = await sourceJson(env, `/reviews/v1/subjects/${id}/summary`, signal);
    const pages = []; let after = null;
    for (let i = 0; i < 20; i++) {
      const params = new URLSearchParams({ limit: '20' });
      if (after !== null) { params.set('after', after); params.set('snapshot', summary.snapshot_id); }
      const page = await sourceJson(env, `/reviews/v1/subjects/${id}/contributions?${params}`, signal);
      pages.push({ ...page, request_after: after });
      if (!page.has_more) break;
      after = page.next_after;
    }
    return normalizeProjection({ subject: approved.subject, summary, pages });
  } catch { return null; }
}
export default {
  async fetch(request, env) {
    if (env.PUBLICATION_MODE !== 'test' || env.PUBLICATION_ENVIRONMENT !== 'test' || !env.PUBLICATION_DB) return fail(404);
    const url = new URL(request.url), db = env.PUBLICATION_DB;
    if (url.pathname.startsWith('/__test/')) {
      if (request.method !== 'POST') return fail(405);
      if (!authorized(request, env)) return fail(401);
      try {
        const payload = await bodyJson(request);
        if (url.pathname === '/__test/state') {
          const state = await db.withSession('first-primary').prepare('SELECT digest,state,epoch FROM publication_gate WHERE singleton=1').first();
          return Response.json(state || {}, { headers: noStore });
        }
        if (url.pathname === '/__test/publish') return await publish(db, payload, env.PUBLICATION_CANONICAL_ORIGIN, env);
        if (url.pathname === '/__test/withdraw') return await withdraw(db, payload.expected_digest, payload.record_id, payload.expected_epoch);
        if (url.pathname === '/__test/recover') return await recover(db, payload, env);
        return fail(404);
      } catch { return fail(400, 'invalid request'); }
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') return fail(405);
    if (!['/publication/html', '/publication/jsonld'].includes(url.pathname) && !/^\/publication\/media\/[a-f0-9]{64}$/.test(url.pathname)) return fail(404);
    try {
      // A single primary read binds gate state and immutable artifact together.
      const row = await db.withSession('first-primary').prepare('SELECT g.state,g.digest,g.epoch,a.source_origin,a.source_scope,a.canonical_origin,a.projection,a.html,a.jsonld,a.media FROM publication_gate g JOIN publication_artifacts a ON a.digest=g.digest WHERE g.singleton=1').first();
      if (!row || row.state !== 'active') return new Response(request.method === 'HEAD' ? null : fallback, { status: 503, headers: { ...noStore, 'Content-Type': 'text/html; charset=utf-8' } });
      if (env.PUBLICATION_CANONICAL_ORIGIN !== row.canonical_origin || !await sourceCurrent(env, JSON.parse(row.projection), row.source_origin, row.source_scope)) {
        await quarantine(db, row);
        return new Response(request.method === 'HEAD' ? null : fallback, { status: 503, headers: { ...noStore, 'Content-Type': 'text/html; charset=utf-8' } });
      }
      const current = await db.withSession('first-primary').prepare('SELECT state,digest,epoch FROM publication_gate WHERE singleton=1').first();
      if (!current || current.state !== 'active' || current.digest !== row.digest || current.epoch !== row.epoch) return new Response(request.method === 'HEAD' ? null : fallback, { status: 503, headers: { ...noStore, 'Content-Type': 'text/html; charset=utf-8' } });
      const headers = { ...noStore, 'X-Publication-Digest': row.digest };
      if (url.pathname === '/publication/html') return new Response(request.method === 'HEAD' ? null : row.html, { headers: { ...headers, 'Content-Type': 'text/html; charset=utf-8' } });
      if (url.pathname === '/publication/jsonld') return new Response(request.method === 'HEAD' ? null : row.jsonld, { headers: { ...headers, 'Content-Type': 'application/ld+json; charset=utf-8' } });
      const media = JSON.parse(row.media).find((item) => item.sha256 === url.pathname.split('/').at(-1));
      if (!media) return fail(404);
      const bytes = Uint8Array.from(atob(media.data), (c) => c.charCodeAt(0));
      return new Response(request.method === 'HEAD' ? null : bytes, { headers: { ...headers, 'Content-Type': media.type } });
    } catch { return new Response(request.method === 'HEAD' ? null : fallback, { status: 503, headers: { ...noStore, 'Content-Type': 'text/html; charset=utf-8' } }); }
  }
};
