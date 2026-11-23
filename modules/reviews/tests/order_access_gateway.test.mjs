import test from 'node:test';
import assert from 'node:assert/strict';
import gateway from '../gateway/worker.mjs';

test('order gateway forwards only the customer body and edge address without following redirects', async () => {
  let seen;
  const env = {ORDER_ACCESS_GATEWAY:'test', REVIEWS_STOREFRONT_ORIGIN:'https://shop.example',
    CARTRAY_SOURCE:{fetch:async request => { seen = request; return new Response(null, {status:302,
      headers:{Location:'https://files.example/resource'}}); }}};
  const request = new Request('https://shop.example/order-access', {method:'POST',
    headers:{'Origin':'https://shop.example','Cookie':'session=private','Authorization':'Bearer private',
      'Content-Type':'application/json','CF-Connecting-IP':'192.0.2.1'}, body:'{"token":"capability"}'});
  const result = await gateway.fetch(request, env);
  assert.equal(result.status, 302);
  assert.equal(result.headers.get('location'), 'https://files.example/resource');
  assert.equal(seen.redirect, 'manual');
  assert.equal(seen.headers.get('cookie'), null);
  assert.equal(seen.headers.get('authorization'), null);
  assert.equal(seen.headers.get('cf-connecting-ip'), '192.0.2.1');
  assert.equal(await seen.text(), '{"token":"capability"}');
  assert.equal((await gateway.fetch(new Request(request.url, {method:'PUT'}), env)).status, 405);
  assert.equal((await gateway.fetch(new Request(request.url + '?token=secret'), env)).status, 403);
  assert.equal((await gateway.fetch(new Request(request.url), {...env, ORDER_ACCESS_GATEWAY:'disabled'})).status, 404);
});

test('human and one-click unsubscribe work without fulfilment or browser credentials', async () => {
  const calls=[];
  const env={ORDER_ACCESS_GATEWAY:'test',REVIEWS_STOREFRONT_ORIGIN:'https://shop.example',
    CARTRAY_SOURCE:{fetch:async request=>{calls.push(request);return new Response('unsubscribed')}}};
  const url='https://shop.example/review-unsubscribe?token='+'a'.repeat(64);
  assert.equal((await gateway.fetch(new Request(url),env)).status,200);
  assert.equal((await gateway.fetch(new Request(url,{method:'POST',headers:{
    'Content-Type':'application/x-www-form-urlencoded','Cookie':'private=secret','Authorization':'Bearer browser'},
    body:'List-Unsubscribe=One-Click'}),env)).status,200);
  assert.equal(calls.length,2);
  assert.equal(calls[1].headers.get('origin'),null);
  assert.equal(calls[1].headers.get('cookie'),null);
  assert.equal(calls[1].headers.get('authorization'),null);
  assert.equal(await calls[1].text(),'List-Unsubscribe=One-Click');
});

test('same-origin source prefix is stripped for order access and one-click unsubscribe',async()=>{
  const seen=[];const env={ORDER_ACCESS_GATEWAY:'test',REVIEWS_STOREFRONT_ORIGIN:'https://shop.example',
    CARTRAY_SOURCE_API_PATH:'/api',CARTRAY_SOURCE:{async fetch(request){seen.push(new URL(request.url));return new Response('ok');}}};
  for(const method of ['GET','POST'])for(const path of ['/order-access','/review-unsubscribe?token='+'a'.repeat(64)]){
    const response=await gateway.fetch(new Request('https://shop.example/api'+path,{method,
      headers:path.startsWith('/order-access')?{Origin:'https://shop.example'}:{},...(method==='POST'?{body:'x'}:{})}),env);
    assert.equal(response.status,200);assert.equal(seen.at(-1).pathname,path.split('?')[0]);
    if(path.includes('?'))assert.equal(seen.at(-1).search,'?token='+'a'.repeat(64));
  }
});
