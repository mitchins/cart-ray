// Local-only synthetic R4 outage simulator. Never bind to an external interface.
import http from 'node:http';
const upstream = new URL(process.argv[2]), port = Number(process.argv[3]);
if (upstream.protocol !== 'http:' || upstream.hostname !== '127.0.0.1' || !Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('local test only');
let offline = false;
const counts = {};
http.createServer(async (request, response) => {
  const path = new URL(request.url, 'http://127.0.0.1');
  if (request.method === 'GET' && path.pathname === '/__test/counts') { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(counts)); return; }
  if (request.method === 'POST' && ['/__test/off', '/__test/on'].includes(path.pathname)) {
    offline = path.pathname.endsWith('/off'); response.writeHead(204); response.end(); return;
  }
  if (request.method !== 'GET') { response.writeHead(405); response.end(); return; }
  if (offline) { response.writeHead(503, { 'Cache-Control': 'no-store' }); response.end(); return; }
  counts[path.pathname] = (counts[path.pathname] || 0) + 1;
  try {
    const result = await fetch(new URL(path.pathname + path.search, upstream), { cache: 'no-store' });
    response.writeHead(result.status, { 'Content-Type': 'application/json', 'Cache-Control': result.headers.get('cache-control') || 'no-store' });
    response.end(Buffer.from(await result.arrayBuffer()));
  } catch { response.writeHead(503, { 'Cache-Control': 'no-store' }); response.end(); }
}).listen(port, '127.0.0.1');
