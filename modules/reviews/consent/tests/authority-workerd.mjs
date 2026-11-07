import {authority,checkoutWithContext} from '../authority.mjs';
let deliveredCode;
export default {async fetch(request, env) {
 const url=new URL(request.url); if(url.pathname==='/__test/code') return Response.json({code:deliveredCode}); const scope={ACORN_PURCHASE_CONTEXT:'test',REVIEWS_STORE_KEY:'acorn-store',REVIEWS_ENVIRONMENT:'test',STOREFRONT_ORIGIN:'https://acorn.test',CONTEXT_DB:env.CONTEXT_DB,
 PURCHASE_CODE_KEY:'z'.repeat(40), PURCHASE_TRANSPORT_TOKEN:'t'.repeat(40), PURCHASE_EMAIL_TRANSPORT:{fetch:async(_url,opts)=>{deliveredCode=JSON.parse(opts.body).code; return new Response(null,{status:204})}}, CHECKOUT:{fetch:async()=>Response.json({session_id:'cs_test_abcdefghijk123456',checkout_url:'https://checkout.stripe.com/test'})},
 PURCHASE_EVIDENCE:{fetch:async(_url,opts)=>Response.json({context_hash:JSON.parse(opts.body).context_hash,recipient_possession_verified:true,store:'acorn-store',environment:'test',session_id:'cs_test_abcdefghijk123456',confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',outbox_id:12,acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:"v1"})}};
 return url.pathname==='/checkout' ? checkoutWithContext(request,scope) : authority(request,scope);
}};
