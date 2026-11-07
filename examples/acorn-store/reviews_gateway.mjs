/** Explicitly enabled Acorn test Pages gateway for customer invitation actions. */
const actions = new Set(['/reviews/v1/invitation', '/reviews/v1/invitation-recovery', '/reviews/v1/contributions']);
const management = /^\/reviews\/v1\/manage\/[a-f0-9]{64}\/(?:read|edit|withdraw)$/;
const unavailable = status => new Response('unavailable', {status, headers:{'Cache-Control':'no-store'}});
export default {async fetch(request, env) {
  const url = new URL(request.url);
  const reviewRoute = url.pathname.startsWith('/reviews/');
  if (!reviewRoute) {
    if (url.pathname.startsWith('/reviews-admin/') || url.pathname.startsWith('/reviews-internal/')) return unavailable(404);
    return env.ASSETS?.fetch ? env.ASSETS.fetch(request) : unavailable(503);
  }
  if (/^\/reviews\/v1\/subjects\/[A-Za-z0-9_-]{1,128}\/(?:summary|contributions)$/.test(url.pathname)) {
    if (request.method !== 'GET') return unavailable(405);
    if (env.ACORN_REVIEWS_GATEWAY !== 'test' || !env.ACORN_STOREFRONT_ORIGIN ||
        url.origin !== env.ACORN_STOREFRONT_ORIGIN) return unavailable(403);
    if (url.search.length > 1024 || env.ACORN_REVIEWS_READ_MODE !== 'test' || !env.REVIEWS_PUBLIC?.fetch)
      return unavailable(503);
    try {
      // Public reads must never forward browser cookies, capabilities or staff credentials.
      return await env.REVIEWS_PUBLIC.fetch(new Request(request.url, {method:'GET', headers:{Accept:'application/json'}}));
    } catch { return unavailable(503); }
  }
  if (url.search || !(actions.has(url.pathname) || management.test(url.pathname))) return unavailable(404);
  if (request.method !== 'POST') return unavailable(405);
  if (env.ACORN_REVIEWS_GATEWAY !== 'test' || !env.ACORN_STOREFRONT_ORIGIN ||
      url.origin !== env.ACORN_STOREFRONT_ORIGIN || request.headers.get('Origin') !== env.ACORN_STOREFRONT_ORIGIN)
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
