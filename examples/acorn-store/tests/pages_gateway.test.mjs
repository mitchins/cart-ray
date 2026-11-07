import {test} from 'node:test';
import assert from 'node:assert/strict';
import pages from '../pages_gateway.mjs';
const origin='https://acorn.test';
const req=(path,options={})=>new Request(origin+path,options);
function env(){return {ACORN_STOREFRONT_ORIGIN:origin,ASSETS:{fetch:async()=>new Response('asset')},CHECKOUT:{fetch:async(request)=>Response.json({direct:true,body:await request.text()})},CONSENT_GATEWAY:{fetch:async()=>Response.json({gateway:true})}};}
test('only designated paths reach the gateway and private internals stay inaccessible',async()=>{
 const e=env(); for(const p of ['/confirmed','/delivery-recipient','/send','/reviews-internal/v1/consents','/review-consent/v1/nope']) assert.equal((await pages.fetch(req(p,{method:'POST'}),e)).status,404);
 assert.equal(await (await pages.fetch(req('/'),e)).text(),'asset');
 assert.equal((await pages.fetch(req('/review-consent/v1/context'),e)).status,405);
 assert.equal((await pages.fetch(new Request('https://other.test/checkout',{method:'POST'}),e)).status,403);
});
test('checkout falls back without changing the body if optional gateway fails',async()=>{
 for(const mode of ['missing','error','503']) {
  const e=env(); if(mode==='missing') delete e.CONSENT_GATEWAY;
  if(mode==='error') e.CONSENT_GATEWAY.fetch=async()=>{throw new Error('unavailable')};
  if(mode==='503') e.CONSENT_GATEWAY.fetch=async()=>new Response(null,{status:503});
  const r=await pages.fetch(req('/checkout',{method:'POST',body:'synthetic payload'}),e);
  assert.equal(r.status,200);assert.deepEqual(await r.json(),{direct:true,body:'synthetic payload'});
  assert.equal((await pages.fetch(req('/review-consent/v1/context',{method:'POST',body:'{}'}),e)).status,503);
 }
});
