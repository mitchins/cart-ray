import {test} from 'node:test';
import assert from 'node:assert/strict';
import gateway from '../reviews_gateway.mjs';
const origin='https://store.example';
const request=(path,options={})=>new Request(origin+path,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json'},body:'{}',...options});
function env(){return {ACORN_REVIEWS_GATEWAY:'test',ACORN_STOREFRONT_ORIGIN:origin, REVIEWS_API:{fetch:async r=>new Response(new URL(r.url).pathname,{status:202})}, ASSETS:{fetch:async()=>new Response('asset')}};}
test('only customer POST routes reach the private Reviews service',async()=>{
 const e=env(); for(const path of ['/reviews/v1/invitation','/reviews/v1/invitation-recovery','/reviews/v1/contributions',...['read','edit','withdraw'].map(a=>`/reviews/v1/manage/${'a'.repeat(64)}/${a}`)]) assert.equal((await gateway.fetch(request(path),e)).status,202);
 for(const path of ['/reviews-admin/v1/subjects','/reviews-internal/v1/automation-initial-prepare','/reviews/v1/unrelated','/reviews/v1/invitation?x=1']) assert.equal((await gateway.fetch(request(path),e)).status,404);
});
test('test mode, exact origin, method, and private binding fail closed',async()=>{
 const e=env(); assert.equal((await gateway.fetch(new Request(origin+'/reviews/v1/invitation'),e)).status,405);
 assert.equal((await gateway.fetch(request('/reviews/v1/invitation',{headers:{Origin:'https://other.example'}}),e)).status,403);
 e.ACORN_REVIEWS_GATEWAY='disabled';assert.equal((await gateway.fetch(request('/reviews/v1/invitation'),e)).status,403);
 e.ACORN_REVIEWS_GATEWAY='test'; delete e.REVIEWS_API;assert.equal((await gateway.fetch(request('/reviews/v1/invitation'),e)).status,503);
});
test('ordinary storefront assets remain available',async()=>assert.equal(await (await gateway.fetch(new Request(origin+'/review/'),env())).text(),'asset'));

test('public summaries and paginated contributions are bounded and stripped of credentials',async()=>{
 let calls=0; const e=env(); e.ACORN_REVIEWS_READ_MODE='test'; e.REVIEWS_PUBLIC={fetch:async r=>{
  calls++;assert.equal(r.method,'GET');assert.equal(r.headers.get('cookie'),null);assert.equal(r.headers.get('authorization'),null);
  assert.equal(r.headers.get('accept'),'application/json'); return new Response(new URL(r.url).search,{status:200});}};
 const headers={Cookie:'private=1',Authorization:'Bearer private'};
 assert.equal((await gateway.fetch(new Request(origin+'/reviews/v1/subjects/acorn-growing-kit/summary',{headers}),e)).status,200);
 const result=await gateway.fetch(new Request(origin+'/reviews/v1/subjects/acorn-growing-kit/contributions?limit=10&after=cursor&snapshot=revision',{headers}),e);
 assert.equal(result.status,200);assert.equal(await result.text(),'?limit=10&after=cursor&snapshot=revision');assert.equal(calls,2);
 assert.equal((await gateway.fetch(new Request(origin+'/reviews/v1/subjects/acorn-growing-kit/contributions?x='+('x'.repeat(1024))),e)).status,503);
 assert.equal((await gateway.fetch(request('/reviews/v1/subjects/acorn-growing-kit/summary'),e)).status,405);
 e.ACORN_REVIEWS_READ_MODE='disabled';assert.equal((await gateway.fetch(new Request(origin+'/reviews/v1/subjects/acorn-growing-kit/summary'),e)).status,503);
 assert.equal(calls,2);
});

test('customer POST forwards only required protocol headers and body',async()=>{
 const e=env();let observed=0;
 e.REVIEWS_API={fetch:async r=>{
  observed++;assert.equal(r.headers.get('cookie'),null);assert.equal(r.headers.get('authorization'),null);
  assert.equal(r.headers.get('cf-access-jwt-assertion'),null);assert.equal(r.headers.get('x-unrelated'),null);
  assert.equal(r.headers.get('origin'),origin);assert.equal(r.headers.get('content-type'),'application/json');
  assert.equal(r.headers.get('content-length'),'12');assert.equal(r.headers.get('idempotency-key'),'request-123');
  assert.equal(r.headers.get('cf-connecting-ip'),'192.0.2.5');assert.equal(await r.text(),'{"rating":5}');
  return new Response('ok');
 }};
 const headers={Origin:origin,'Content-Type':'application/json','Content-Length':'12','Idempotency-Key':'request-123',
  'CF-Connecting-IP':'192.0.2.5',Cookie:'secret=1',Authorization:'Bearer private',
  'Cf-Access-Jwt-Assertion':'private','X-Unrelated':'private'};
 const response=await gateway.fetch(request('/reviews/v1/contributions',{headers,body:'{"rating":5}'}),e);
 assert.equal(response.status,200);assert.equal(observed,1);
});
