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
  if (url.search || !(actions.has(url.pathname) || management.test(url.pathname))) return unavailable(404);
  if (request.method !== 'POST') return unavailable(405);
  if (env.ACORN_REVIEWS_GATEWAY !== 'test' || !env.ACORN_STOREFRONT_ORIGIN ||
      url.origin !== env.ACORN_STOREFRONT_ORIGIN || request.headers.get('Origin') !== env.ACORN_STOREFRONT_ORIGIN)
    return unavailable(403);
  if (!env.REVIEWS_API?.fetch) return unavailable(503);
  try { return await env.REVIEWS_API.fetch(request); } catch { return unavailable(503); }
}};
