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
