import {authority,checkoutWithContext} from '../authority.mjs';
import {handleConsent} from '../capture.mjs';
import pages from '../../../../examples/acorn-store/pages_gateway.mjs';
let deliveredCode;
export default {async fetch(request, env) {
 const url=new URL(request.url); if(url.pathname==='/__test/code') return Response.json({code:deliveredCode});
 const scope={ACORN_PURCHASE_CONTEXT:'test',REVIEWS_STORE_KEY:'acorn-store',REVIEWS_ENVIRONMENT:'test',STOREFRONT_ORIGIN:'https://acorn.test',CONTEXT_DB:env.CONTEXT_DB,
 PURCHASE_CODE_KEY:'z'.repeat(40), PURCHASE_TRANSPORT_TOKEN:'t'.repeat(40), PURCHASE_EMAIL_TRANSPORT:{fetch:async(_url,opts)=>{deliveredCode=JSON.parse(opts.body).code; return new Response(null,{status:204})}}, CHECKOUT:{fetch:async(req)=>{if(req.method==='POST') await req.text(); return Response.json({session_id:'cs_test_abcdefghijk123456',checkout_url:'https://checkout.stripe.com/test'})}},
 PURCHASE_EVIDENCE:{fetch:async(_url,opts)=>Response.json({context_hash:JSON.parse(opts.body).context_hash,store:'acorn-store',environment:'test',session_id:'cs_test_abcdefghijk123456',confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',outbox_id:12,acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:'v1'})}};
 const reviews={fetch:async()=>Response.json({status:'recorded'})};
 const gateway={fetch:async(req)=>new URL(req.url).pathname==='/checkout' ? checkoutWithContext(req,scope) : handleConsent(req,{...scope,REVIEW_CONSENT_ENABLED:'test',REVIEWS_CONSENT_SECRET:'c'.repeat(40),REVIEWS_CONSENT_RATE_KEY:'r'.repeat(40),REVIEWS_RECIPIENT_EVIDENCE_SECRET:'p'.repeat(40),REVIEW_CONSENT_LIMITER:{limit:async()=>({success:true})},REVIEW_CONSENT_AUTHORITY:{fetch:(u,o)=>authority(new Request(u,o),scope)},REVIEWS:reviews})};
 const data=request.method==='POST' ? await request.arrayBuffer() : undefined;
 const sameOrigin=new Request('https://acorn.test'+url.pathname,{method:request.method,headers:request.headers,body:data});
 return pages.fetch(sameOrigin,{ACORN_STOREFRONT_ORIGIN:'https://acorn.test',CONSENT_GATEWAY:gateway,CHECKOUT:scope.CHECKOUT});
}};
