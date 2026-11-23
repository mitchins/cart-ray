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
