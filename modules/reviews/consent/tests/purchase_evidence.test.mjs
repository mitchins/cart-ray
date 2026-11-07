import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHmac,webcrypto} from 'node:crypto';
import {purchaseEvidence} from '../purchase_evidence.mjs';
globalThis.crypto ??= webcrypto;
const context='a'.repeat(64), session='cs_test_abcdefghijk123456', order='cr_order_1', catalogue='sha256:'+'c'.repeat(64), mapping='d'.repeat(64), attkey='1'.repeat(64);
function setup() {
 const now=Math.floor(Date.now()/1000), calls=[];
 const row={order_id:order,manifest_version:catalogue,currency:'aud',subtotal_minor:4200,items_digest:'abc123',external_session_id:session,settlement_event_id:'evt_test_1',stripe_payment_status:'paid',stripe_amount_total_minor:4200,outbox_id:12,payload_json:JSON.stringify({order_id:order,session_id:session,items_digest:'abc123'})};
 const item={stripe_price_id:'price_test_1',quantity:1,unit_amount_minor:4200};
 let purchase={id:session,livemode:false,status:'complete',payment_status:'paid',amount_total:4200,currency:'aud',metadata:{cr_order_id:order,cr_catalogue_version:catalogue,cr_items_digest:'abc123'},customer_details:{email:'squirrel@example.com'}};
 const db={prepare(sql){return {bind(){return {async first(){return row},async all(){return {results:[item]}}}}}}};
 const contexts={prepare(){return {bind(){return {async first(){return {checkout_session_id:session}}}}}}};
 const env={PURCHASE_EVIDENCE_MODE:'test',STORE_KEY:'acorn-store',ENVIRONMENT:'test',COMMERCE_DB:db,CONTEXT_DB:contexts,
 CF_VERSION_METADATA:{id:'authority-uuid'},REVIEWS_API:{fetch:async()=>Response.json({state:'held_eligibility_recipient_consent'})},REVIEWS_AUTOMATION_INGRESS_TOKEN:'i'.repeat(40),STRIPE_TEST_KEY:'sk_test_synthetic',PURCHASE_ATTESTATION_TOKEN:'x'.repeat(40),PURCHASE_ATTESTATION_KEY:attkey,RECIPIENT_HASH_KEY:'y'.repeat(40),RECIPIENT_RESOLVE_TOKEN:'q'.repeat(40),SOURCE_WORKER_VERSION:'source-v',REVIEWS_WORKER_VERSION:'reviews-v',PRODUCER_WORKER_VERSION:'producer-v',APPROVED_TEST_EMAIL:'squirrel@example.com',INSTALLATION_ID:'install_test',STRIPE_ACCOUNT_ID:'acct_test123',CATALOGUE_VERSION:catalogue,MAPPING_SHA256:mapping,COMMERCE_DB_ID:'db-commerce',CONTEXT_DB_ID:'db-context',
 ATTESTOR:{async fetch(_url,opts){const {authority_version,nonce}=JSON.parse(opts.body);const proof=['cartray-reviews-purchase-attestation-v1','acorn-store','test','install_test','db-commerce','db-context','acct_test123',catalogue,mapping,'source-v','reviews-v','producer-v',authority_version,nonce,now,now+30];const signature=createHmac('sha256',Buffer.from(attkey,'hex')).update(JSON.stringify(proof)).digest('hex');return Response.json({proof,signature})}}};
 const fetcher=async(url)=>{calls.push(url); if(url.endsWith('/v1/account')) return Response.json({id:'acct_test123'}); if(url.includes('/line_items?')) return Response.json({has_more:false,data:[{price:{id:'price_test_1',unit_amount:4200},quantity:1}]}); return Response.json(purchase)};
 const call=()=>purchaseEvidence(new Request('https://private/confirmed',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({session_id:session,context_hash:context})}),env,fetcher);
 return {env,row,call,calls,fetcher,setPurchase(x){purchase=x}};
}
test('confirmed private Stripe and D1 evidence returns scoped opaque recipient without email',async()=>{
 const {call,calls}=setup(), r=await call(), x=await r.json(); assert.equal(r.status,200); assert.equal(x.acquisition_id,order); assert.equal(x.context_hash,context); assert.match(x.recipient_context_hash,/^[a-f0-9]{64}$/); assert.match(x.recipient_reference,/^ref_[a-f0-9]{64}$/); assert.equal('email' in x,false); assert.equal(calls.length,3);
});
test('mismatched scope, account, currency, session and recipient fail closed',async()=>{
 for(const alter of [x=>{x.env.STORE_KEY='other'},x=>{x.env.STRIPE_ACCOUNT_ID='acct_other'},x=>{x.row.manifest_version='sha256:'+'f'.repeat(64)},x=>{x.row.stripe_payment_status='unpaid'},x=>{x.env.APPROVED_TEST_EMAIL='different@example.com'},x=>{x.env.CONTEXT_DB.prepare=()=>({bind:()=>({first:async()=>({checkout_session_id:'cs_test_wrong'})})})}]) {
  const x=setup(); alter(x); assert.equal((await x.call()).status,503);
 }
});
test('untrusted or expired installation proof fails closed before Stripe',async()=>{
 const x=setup(); x.env.ATTESTOR.fetch=async()=>Response.json({proof:[],signature:'0'.repeat(64)});
 assert.equal((await x.call()).status,503); assert.equal(x.calls.length,0);
});

test('recipient address is available only over the private authorized transport route',async()=>{
 const x=setup(); const initial=await (await x.call()).json();
 const request=(token,ref)=>new Request('https://private/delivery-recipient',{method:'POST',headers:{'content-type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({context_hash:context,recipient_reference:ref})});
 assert.equal((await purchaseEvidence(request('wrong',initial.recipient_reference),x.env,x.fetcher)).status,503);
 assert.equal((await purchaseEvidence(request(x.env.RECIPIENT_RESOLVE_TOKEN,'ref_wrong'),x.env,x.fetcher)).status,503);
 const r=await purchaseEvidence(request(x.env.RECIPIENT_RESOLVE_TOKEN,initial.recipient_reference),x.env,x.fetcher); assert.equal(r.status,200); assert.deepEqual(await r.json(),{email:'squirrel@example.com'});
});
test('confirmed reconciliation uses CartRay canonical derived event ID without claiming a Stripe event',async()=>{
 const x=setup(); x.row.settlement_event_id=null;
 const r=await x.call(), data=await r.json(); assert.equal(r.status,200); assert.equal(data.event_id,'recon_'+order);
});
test('Stripe calls reject redirects and never send credentials to a redirected destination',async()=>{
 const x=setup(), seen=[];
 const fetcher=async(url,opts)=>{seen.push([url,opts.redirect]); return new Response(null,{status:302,headers:{Location:'https://other.example/'}})};
 const request=new Request('https://private/confirmed',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({session_id:session,context_hash:context})});
 assert.equal((await purchaseEvidence(request,x.env,fetcher)).status,503);
 assert.deepEqual(seen,[['https://api.stripe.com/v1/account','manual']]);
});
test('missing or non-held Reviews intake rejects before consulting Stripe or sending a code',async()=>{
 const x=setup(); x.env.REVIEWS_API.fetch=async()=>new Response(null,{status:409});
 assert.equal((await x.call()).status,503); assert.equal(x.calls.length,0);
 x.env.REVIEWS_API.fetch=async()=>Response.json({state:'sent'});
 assert.equal((await x.call()).status,503); assert.equal(x.calls.length,0);
});
