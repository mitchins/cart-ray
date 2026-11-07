import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DatabaseSync} from 'node:sqlite';
import {webcrypto} from 'node:crypto';
import {authority, checkoutWithContext} from '../authority.mjs';
globalThis.crypto ??= webcrypto;
const ORIGIN='https://acorn.test', SESSION='cs_test_abcdefghijk123456';
function setup() {
 const db=new DatabaseSync(':memory:'); db.exec(readFileSync(new URL('../migrations/0001_purchaser_context.sql',import.meta.url),'utf8'));
 const d1={prepare(sql){return {bind(...v){return {async run(){return db.prepare(sql).run(...v)}, async first(){return db.prepare(sql).get(...v) ?? null}}}}}};
 let sentCode; let purchase={recipient_reference:'recipient_ref_1',store:'acorn-store',environment:'test',session_id:SESSION,confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1};
 const env={ACORN_PURCHASE_CONTEXT:'test',REVIEWS_STORE_KEY:'acorn-store',REVIEWS_ENVIRONMENT:'test',STOREFRONT_ORIGIN:ORIGIN,CONTEXT_DB:d1,
 PURCHASE_CODE_KEY:'z'.repeat(40), PURCHASE_EMAIL_TRANSPORT:{fetch:async(_url,opts)=>{sentCode=JSON.parse(opts.body).code; return new Response(null,{status:204})}}, CHECKOUT:{fetch:async()=>Response.json({session_id:SESSION,checkout_url:'https://checkout.stripe.com/test'})},
 PURCHASE_EVIDENCE:{fetch:async(_url,opts)=>Response.json({...purchase,context_hash:JSON.parse(opts.body).context_hash,recipient_possession_verified:true})}};
 return {db,env,setPurchase(x){purchase=x},getCode(){return sentCode}};
}
const checkout=(env,body='{}')=>checkoutWithContext(new Request(ORIGIN+'/checkout',{method:'POST',headers:{Origin:ORIGIN,'content-type':'application/json'},body}),env);
const consent=(env,cookie,path='context',body='{}')=>authority(new Request('https://internal/review-consent/v1/'+path,{method:'POST',headers:{'content-type':'application/json',Cookie:cookie},body}),env);
test('purchase cookie is opaque, first-party and grants idempotently only for confirmed source',async()=>{
 const {db,env,getCode}=setup(); const r=await checkout(env); const cookie=r.headers.get('set-cookie'); assert.match(cookie,/^__Host-acorn-purchase=[a-f0-9]{64}; Path=\/; Max-Age=7200; Secure; HttpOnly; SameSite=Lax$/);
 assert.equal((await consent(env,cookie)).status,200);
 assert.equal((await consent(env,cookie,'challenge','{}')).status,200);
 assert.equal((await consent(env,cookie,'verify',JSON.stringify({code:getCode()}))).status,200);
 const a=await consent(env,cookie,'grant','{"accept":true}'); const b=await consent(env,cookie,'grant','{"accept":true}');
 assert.equal(a.status,200); assert.deepEqual(await a.json(),await b.json()); assert.equal(db.prepare('SELECT count(*) c FROM purchaser_consent_grants').get().c,1);
 assert.equal((await consent(env,'__Host-acorn-purchase='+SESSION)).status,503);
});
test('unknown, expired, unconfirmed, mis-scoped, changed recipient and tampered requests fail closed',async()=>{
 const {db,env,setPurchase,getCode}=setup(); let r=await checkout(env); let cookie=r.headers.get('set-cookie');
 assert.equal((await consent(env,'')).status,503);
 assert.equal((await consent(env,cookie,'grant','{"accept":true,"event_id":"forged"}')).status,503);
 setPurchase({store:'other',environment:'test',session_id:SESSION,confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1});
 assert.equal((await consent(env,cookie)).status,503);
 setPurchase({store:'acorn-store',environment:'test',session_id:SESSION,confirmed:false,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1});
 assert.equal((await consent(env,cookie)).status,503);
 setPurchase({store:'acorn-store',environment:'test',session_id:SESSION,confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1});
 assert.equal((await consent(env,cookie,'challenge','{}')).status,200);
 assert.equal((await consent(env,cookie,'verify',JSON.stringify({code:getCode()}))).status,200);
 assert.equal((await consent(env,cookie,'grant','{"accept":true}')).status,200);
 setPurchase({store:'acorn-store',environment:'test',session_id:SESSION,confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'b'.repeat(64),binding_version:2});
 assert.equal((await consent(env,cookie,'grant','{"accept":true}')).status,503);
 db.prepare('UPDATE purchaser_contexts SET created_at=1,expires_at=2').run(); assert.equal((await consent(env,cookie)).status,503);
});
test('checkout continues without authority, database or with live session; browser cannot supply context',async()=>{
 const {env}=setup(); delete env.PURCHASE_EVIDENCE; let r=await checkout(env); assert.equal(r.status,200); assert.equal(r.headers.get('set-cookie'),null);
 env.PURCHASE_EVIDENCE={fetch:async()=>Response.json({})}; env.CHECKOUT.fetch=async()=>Response.json({session_id:'cs_live_abcdefghijk'}); r=await checkout(env); assert.equal(r.status,200); assert.equal(r.headers.get('set-cookie'),null);
 assert.equal((await checkoutWithContext(new Request(ORIGIN+'/checkout',{method:'POST',headers:{Origin:'https://evil.test'},body:'{}'}),env)).headers.get('set-cookie'),null);
});
test('expiry, source outage, malformed evidence and duplicate cookies are denied',async()=>{
 const {db,env,setPurchase}=setup(); const cookie=(await checkout(env)).headers.get('set-cookie');
 assert.equal((await consent(env,cookie+'; '+cookie)).status,503);
 env.PURCHASE_EVIDENCE.fetch=async()=>new Response('x'.repeat(5000));
 assert.equal((await consent(env,cookie)).status,503);
 env.PURCHASE_EVIDENCE.fetch=async()=>{throw new Error('offline')};
 assert.equal((await consent(env,cookie)).status,503);
 env.PURCHASE_EVIDENCE.fetch=async()=>Response.json({store:'acorn-store',environment:'test',session_id:SESSION,confirmed:true,source_attested:false,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1});
 assert.equal((await consent(env,cookie)).status,503);
 db.prepare('UPDATE purchaser_contexts SET created_at=1,expires_at=2').run();
 assert.equal((await consent(env,cookie)).status,503);
});
test('possession proof is required and bound to this opaque browser context',async()=>{
 const {env}=setup(); const cookie=(await checkout(env)).headers.get('set-cookie');
 const base={store:'acorn-store',environment:'test',session_id:SESSION,confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1};
 env.PURCHASE_EVIDENCE.fetch=async(_url,opts)=>Response.json({...base,context_hash:JSON.parse(opts.body).context_hash});
 assert.equal((await consent(env,cookie,'grant','{"accept":true}')).status,503);
 env.PURCHASE_EVIDENCE.fetch=async()=>Response.json({...base,context_hash:'b'.repeat(64)});
 assert.equal((await consent(env,cookie,'grant','{"accept":true}')).status,503);
});
test('verification limits attempts, binds recipient, and never blindly resends an uncertain delivery',async()=>{
 const one=setup(); const cookie=(await checkout(one.env)).headers.get('set-cookie');
 let sends=0; one.env.PURCHASE_EMAIL_TRANSPORT.fetch=async()=>{sends++; throw new Error('unknown outcome')};
 assert.equal((await consent(one.env,cookie,'challenge','{}')).status,503);
 assert.equal((await consent(one.env,cookie,'challenge','{}')).status,503); assert.equal(sends,1);
 const two=setup(); const c=(await checkout(two.env)).headers.get('set-cookie');
 assert.equal((await consent(two.env,c,'challenge','{}')).status,200);
 const real=two.getCode(); const wrong=real==='000000'?'000001':'000000';
 for(let i=0;i<5;i++) assert.equal((await consent(two.env,c,'verify',JSON.stringify({code:wrong}))).status,503);
 assert.equal((await consent(two.env,c,'verify',JSON.stringify({code:real}))).status,503);
 assert.equal((await consent(two.env,c,'grant','{"accept":true}')).status,503);
});
test('verified challenge cannot transfer across acquisition or issuer provenance',async()=>{
 const {env,setPurchase,getCode}=setup(); const c=(await checkout(env)).headers.get('set-cookie');
 assert.equal((await consent(env,c,'challenge','{}')).status,200);
 const code=getCode();
 const original={store:'acorn-store',environment:'test',session_id:SESSION,confirmed:true,source_attested:true,recipient_reference:'recipient_ref_1',event_id:'evt_test_1',acquisition_id:'acq_test_1',source_provenance:'issuer_v1',recipient_context_hash:'a'.repeat(64),binding_version:1};
 setPurchase({...original,source_provenance:'issuer_v2'});
 assert.equal((await consent(env,c,'verify',JSON.stringify({code}))).status,503);
 setPurchase(original); assert.equal((await consent(env,c,'verify',JSON.stringify({code}))).status,200);
 setPurchase({...original,acquisition_id:'acq_test_2'});
 assert.equal((await consent(env,c,'grant','{"accept":true}')).status,503);
});
