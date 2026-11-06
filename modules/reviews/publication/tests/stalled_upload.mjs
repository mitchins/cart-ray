// Hosted synthetic Worker only: observe incomplete uploads without sending real data.
import tls from 'node:tls';
import assert from 'node:assert/strict';
const base = new URL(process.argv[2]), token = process.env.PUBLICATION_TEST_TOKEN;
if (base.protocol !== 'https:' || !base.hostname.startsWith('r5-gate-') || !base.hostname.endsWith('.workers.dev') || !token) throw new Error('disposable hosted test target required');
function stalled(authorized) {
  let markReady;
  const ready = new Promise((resolve) => { markReady = resolve; });
  const outcome = new Promise((resolve) => {
    const socket = tls.connect(443, base.hostname, { servername: base.hostname });
    let settled = false, sent = false;
    const finish = (result) => { if (settled) return; settled = true; clearTimeout(timer); markReady(); socket.destroy(); resolve(sent ? result : 'setup_failure'); };
    const timer = setTimeout(() => finish('client_timeout'), 9000);
    socket.on('secureConnect', () => {
      socket.write(`POST /__test/publish HTTP/1.1\r\nHost: ${base.hostname}\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n${authorized ? `Authorization: Bearer ${token}\r\n` : ''}Connection: close\r\n\r\n{"partial":`, (error) => {
        if (error) { finish('setup_failure'); return; }
        sent = true; markReady();
      });
    });
    socket.on('data', (chunk) => finish((chunk.toString().match(/^HTTP\/\d(?:\.\d)?\s+\d+/) || ['non-http response'])[0]));
    socket.on('close', () => finish('connection_closed'));
    socket.on('error', (error) => finish(error.code === 'ECONNRESET' ? 'connection_reset' : 'network_error'));
  });
  return { ready, outcome };
}
for (const authorized of [false, true]) {
  const pending = stalled(authorized); await pending.ready;
  const healthy = await fetch(new URL('/publication/html', base), { signal: AbortSignal.timeout(8000), cache: 'no-store' });
  assert.equal(healthy.status, 503, 'empty gate must remain available during incomplete upload');
  const outcome = await pending.outcome;
  if (outcome.startsWith('HTTP/')) assert.match(outcome, authorized ? /^HTTP\/\S+ 400$/ : /^HTTP\/\S+ 401$/);
  else assert.ok(['connection_closed', 'connection_reset'].includes(outcome), `unexpected transport outcome: ${outcome}`);
  for (let i = 0; i < 3; i++) assert.equal((await fetch(new URL('/publication/html', base), { signal: AbortSignal.timeout(8000), cache: 'no-store' })).status, 503);
  console.log(JSON.stringify({ authorized, on_wire: outcome, healthy_status: healthy.status, healthy_after_disconnect: 3 }));
}
const state = await fetch(new URL('/__test/state', base), { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: '{}' });
assert.deepEqual(await state.json(), {}, 'incomplete upload must not mutate the empty gate');
