/** Explicitly enabled test Pages gateway for customer review actions. */
const actions = new Set(['/reviews/v1/invitation', '/reviews/v1/invitation-recovery', '/reviews/v1/contributions']);
const management = /^\/reviews\/v1\/manage\/[a-f0-9]{64}\/(?:read|edit|withdraw)$/;
const unavailable = status => new Response('unavailable', {status, headers:{'Cache-Control':'no-store'}});
export default {async fetch(request, env) {
  const url = new URL(request.url);
  const gateway = env.REVIEWS_GATEWAY ?? env.ACORN_REVIEWS_GATEWAY;
  const origin = env.REVIEWS_STOREFRONT_ORIGIN ?? env.ACORN_STOREFRONT_ORIGIN;
  const readMode = env.REVIEWS_READ_MODE ?? env.ACORN_REVIEWS_READ_MODE;
  if (url.pathname === '/order-access' || url.pathname === '/review-unsubscribe') {
    if (!['GET', 'POST'].includes(request.method)) return unavailable(405);
    if (env.ORDER_ACCESS_GATEWAY !== 'test' || !origin || url.origin !== origin || !env.CARTRAY_SOURCE?.fetch)
      return unavailable(404);
    if (url.pathname === '/review-unsubscribe' ? !/^\?token=[a-f0-9]{64}$/.test(url.search) :
        (url.search || (request.method === 'POST' && request.headers.get('Origin') !== origin))) return unavailable(403);
    const headers = new Headers();
    for (const name of ['CF-Connecting-IP', 'Content-Type', 'Content-Length']) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    const init = {method:request.method, headers, redirect:'manual'};
    if (request.method === 'POST') { init.body = request.body; init.duplex = 'half'; }
    try { return await env.CARTRAY_SOURCE.fetch(new Request(request.url, init)); }
    catch { return unavailable(503); }
  }
  const reviewRoute = url.pathname.startsWith('/reviews/');
  if (!reviewRoute) {
    if (url.pathname.startsWith('/reviews-admin/') || url.pathname.startsWith('/reviews-internal/')) return unavailable(404);
    return env.ASSETS?.fetch ? env.ASSETS.fetch(request) : unavailable(503);
  }
  if (/^\/reviews\/v1\/subjects\/[A-Za-z0-9_-]{1,128}\/(?:summary|contributions)$/.test(url.pathname)) {
    if (request.method !== 'GET') return unavailable(405);
    if (gateway !== 'test' || !origin || url.origin !== origin) return unavailable(403);
    if (url.search.length > 1024 || readMode !== 'test' || !env.REVIEWS_PUBLIC?.fetch)
      return unavailable(503);
    try {
      // Public reads must never forward browser cookies, capabilities or staff credentials.
      const headers = new Headers({Accept:'application/json'});
      const clientAddress = request.headers.get('CF-Connecting-IP');
      if (clientAddress !== null) headers.set('CF-Connecting-IP', clientAddress);
      return await env.REVIEWS_PUBLIC.fetch(new Request(request.url, {method:'GET', headers}));
    } catch { return unavailable(503); }
  }
  if (url.search || !(actions.has(url.pathname) || management.test(url.pathname))) return unavailable(404);
  if (request.method !== 'POST') return unavailable(405);
  if (gateway !== 'test' || !origin || url.origin !== origin || request.headers.get('Origin') !== origin)
    return unavailable(403);
  if (!env.REVIEWS_API?.fetch) return unavailable(503);
  // Forward only the customer protocol and the Cloudflare-provided client IP
  // used by Reviews ingress limits; browser sessions and staff credentials do
  // not belong on the independent Reviews service binding.
  const headers = new Headers();
  for (const name of ['Origin', 'Content-Type', 'Content-Length', 'Idempotency-Key', 'CF-Connecting-IP']) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  try {
    const forwarded = new Request(request.url, {method:'POST', headers, body:request.body, duplex:'half'});
    return await env.REVIEWS_API.fetch(forwarded);
  } catch { return unavailable(503); }
}};
