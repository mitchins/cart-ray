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
test('test Reviews exposes only public reads without forwarding browser credentials',async()=>{
 const e=env(), calls=[];
 e.ACORN_REVIEWS_READ_MODE='test';
 e.REVIEWS_PUBLIC={fetch:async request=>{calls.push(request);return Response.json({contribution_count:3});}};
 for(const path of ['/reviews/v1/subjects/acorn-growing-kit/summary','/reviews/v1/subjects/woodland-planter/contributions?limit=10']) {
  const response=await pages.fetch(req(path,{headers:{cookie:'private=secret',authorization:'Bearer secret'}}),e);
  assert.equal(response.status,200); assert.equal((await response.json()).contribution_count,3);
 }
 assert.equal(calls.length,2);
 for(const request of calls){assert.equal(request.method,'GET');assert.equal(request.headers.get('cookie'),null);assert.equal(request.headers.get('authorization'),null);}
 assert.equal(new URL(calls[1].url).search,'?limit=10');
 for(const path of ['/reviews/v1/subjects/acorn-growing-kit/submit','/reviews/admin','/reviews/v1/subjects/%2f/summary'])
  assert.equal((await pages.fetch(req(path),e)).status,404);
 assert.equal((await pages.fetch(req('/reviews/v1/subjects/acorn-growing-kit/summary',{method:'POST'}),e)).status,405);
 assert.equal((await pages.fetch(new Request('https://other.test/reviews/v1/subjects/acorn-growing-kit/summary'),e)).status,403);
 assert.equal(calls.length,2);
});
test('missing or failed Reviews leaves checkout available',async()=>{
 for(const mode of ['disabled','missing','error']) {
  const e=env();e.ACORN_REVIEWS_READ_MODE=mode==='disabled'?'disabled':'test';
  if(mode!=='missing') e.REVIEWS_PUBLIC={fetch:async()=>{throw new Error('unavailable')}};
  assert.equal((await pages.fetch(req('/reviews/v1/subjects/acorn-growing-kit/summary'),e)).status,503);
  assert.equal((await pages.fetch(req('/checkout',{method:'POST',body:'synthetic'}),e)).status,200);
 }
});
