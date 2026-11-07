import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.mjs';
test('private evidence and transport endpoints are not routed publicly',async()=>{
 const env={CHECKOUT:{fetch:async()=>Response.json({ok:true})}};
 for(const path of ['/confirmed','/delivery-recipient','/send','/reviews-internal/v1/consents']) {
  const r=await worker.fetch(new Request('https://acorn.test'+path,{method:'POST'}),env); assert.equal(r.status,404);
 }
});
test('checkout and catalogue continue through CartRay while consent is unavailable',async()=>{
 const env={CHECKOUT:{fetch:async()=>Response.json({status:'ok'})}};
 assert.equal((await worker.fetch(new Request('https://acorn.test/checkout',{method:'POST',headers:{origin:'https://acorn.test'},body:'{}'}),env)).status,200);
 assert.equal((await worker.fetch(new Request('https://acorn.test/catalogue'),env)).status,200);
 assert.equal((await worker.fetch(new Request('https://acorn.test/review-consent/v1/context',{method:'POST',body:'{}'}),env)).status,503);
});
