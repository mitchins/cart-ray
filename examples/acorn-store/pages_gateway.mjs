/** Pages advanced Worker for the explicit test consent build only. */
const routes = new Map([
  ['/checkout',new Set(['POST','OPTIONS'])],
  ['/catalogue',new Set(['GET','OPTIONS'])],
  ['/checkout-status',new Set(['GET','OPTIONS'])],
  ['/review-consent/v1/context',new Set(['POST'])],
  ['/review-consent/v1/challenge',new Set(['POST'])],
  ['/review-consent/v1/verify',new Set(['POST'])],
  ['/review-consent/v1/grant',new Set(['POST'])],
]);
const fail = status => new Response('unavailable',{status,headers:{'Cache-Control':'no-store'}});
export default {async fetch(request,env) {
  const url=new URL(request.url), methods=routes.get(url.pathname);
  if(!methods) {
    if(url.pathname.startsWith('/review-consent/') || url.pathname.startsWith('/reviews-internal/') ||
       url.pathname==='/confirmed' || url.pathname==='/delivery-recipient' || url.pathname==='/send') return fail(404);
    return env.ASSETS?.fetch ? env.ASSETS.fetch(request) : fail(503);
  }
  if(!methods.has(request.method)) return fail(405);
  if(!env.ACORN_STOREFRONT_ORIGIN || url.origin!==env.ACORN_STOREFRONT_ORIGIN) return fail(403);
  const consent=url.pathname.startsWith('/review-consent/');
  if(consent) {
    if(!env.CONSENT_GATEWAY?.fetch) return fail(503);
    try {return await env.CONSENT_GATEWAY.fetch(request);} catch {return fail(503);}
  }
  // The checkout service owns its own origin checks and request idempotency.
  // If the optional gateway is absent or unavailable, forward directly.
  if(env.CONSENT_GATEWAY?.fetch) {
    try {const result=await env.CONSENT_GATEWAY.fetch(request.clone()); if(result.status!==503) return result;} catch {}
  }
  if(!env.CHECKOUT?.fetch) return fail(503);
  try {return await env.CHECKOUT.fetch(request);} catch {return fail(503);}
}};
