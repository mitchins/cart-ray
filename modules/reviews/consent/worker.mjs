/** Optional first-party test gateway. Never exposes internal evidence endpoints. */
import {checkoutWithContext,authority} from './authority.mjs';
import {handleConsent} from './capture.mjs';
import {purchaseEvidence} from './purchase_evidence.mjs';
import {sendTestCode} from './test_email_transport.mjs';

export default {async fetch(request,env) {
  const url=new URL(request.url);
  if(!env.CHECKOUT?.fetch) return new Response('unavailable',{status:503,headers:{'Cache-Control':'no-store'}});
  const evidence={fetch:(url,options)=>purchaseEvidence(new Request(url,options),env)};
  const transport={fetch:(url,options)=>sendTestCode(new Request(url,options),{...env,PURCHASE_EVIDENCE:evidence})};
  const scope={...env,PURCHASE_EVIDENCE:evidence,PURCHASE_EMAIL_TRANSPORT:transport};
  if(url.pathname==='/checkout') return checkoutWithContext(request,scope);
  if(url.pathname==='/catalogue' || url.pathname==='/checkout-status') return env.CHECKOUT.fetch(request);
  if(url.pathname.startsWith('/review-consent/v1/')) {
    const authorityService={fetch:(url,options)=>authority(new Request(url,options),scope)};
    return handleConsent(request,{...env,REVIEW_CONSENT_AUTHORITY:authorityService,REVIEWS:env.REVIEWS_API});
  }
  return new Response('unavailable',{status:404,headers:{'Cache-Control':'no-store'}});
}};
