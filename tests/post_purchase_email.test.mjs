import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash, webcrypto} from 'node:crypto';
import {handle} from '../modules/post-purchase/transactional-email/worker.mjs';

globalThis.crypto ??= webcrypto;
const action = {environment:'test', merchant:'merchant-one', workflow_key:'download-one',
  config_ref:'private/download', offer_keys:['product-one'], links:['https://www.dropbox.com/t/example'],
  recipient:'recipient@example.test', sender:'sender@example.test', subject:'Test delivery', stripe_account:'acct_test'};
const config = JSON.stringify({actions:[action]});
const env = {DELIVERY_MODE:'test', ENVIRONMENT:'test', HANDLER_TOKEN:'a'.repeat(32), DELIVERY_CONFIG:config,
  APPROVED_TEST_RECIPIENT:'recipient@example.test', APPROVED_TEST_SENDER:'sender@example.test',
  STRIPE_ACCOUNT_ID:'acct_test', STRIPE_API_KEY:'sk_test_example', SMTP2GO_API_KEY:'api-example'};
const body = {merchant:'merchant-one', environment:'test', workflow_key:'download-one', config_ref:'private/download',
  config_digest:createHash('sha256').update(JSON.stringify(action)).digest('hex'), order_id:'order-one', session_id:'cs_test_123',
  items:[['product-one', 2]]};
function request(value = body, token = env.HANDLER_TOKEN) {
  return new Request('https://post-purchase-handler.internal/', {method:'POST',
    headers:{authorization:`Bearer ${token}`}, body:JSON.stringify(value)});
}
function provider(options = {}) {
  const calls = [];
  return {calls, fetch:async (url, init = {}) => {
    calls.push({url, init});
    if (url.endsWith('/v1/account')) return Response.json({id:options.account || 'acct_test'});
    if (url.includes('/v1/checkout/sessions/')) return Response.json({id:'cs_test_123', livemode:false,
      mode:'payment', status:'complete', metadata:{cr_order_id:options.order || 'order-one'},
      payment_status:options.status || 'paid', customer_details:{email:options.recipient || 'recipient@example.test'}});
    if (options.failSend) throw new Error('provider timeout');
    return Response.json({data:{succeeded:1, failed:0, email_id:'receipt-one'}});
  }};
}

test('confirmed approved test purchase delivers configured dummy links', async () => {
  const p = provider();
  const result = await handle(request(), env, p.fetch);
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), {provider_reference:'receipt-one'});
  assert.equal(p.calls.length, 3);
  assert.deepEqual(JSON.parse(p.calls[2].init.body).to, ['recipient@example.test']);
});

test('forged scope, unapproved recipient, wrong account and unpaid purchases never send', async () => {
  for (const [payload, settings, options] of [
    [{...body, config_digest:'0'.repeat(64)}, env, {}],
    [body, {...env, APPROVED_TEST_RECIPIENT:'other@example.test'}, {}],
    [body, env, {account:'acct_other'}],
    [body, env, {status:'unpaid'}],
    [body, env, {recipient:'other@example.test'}],
    [body, env, {order:'another-order'}],
    [{...body, items:[['other',1]]}, env, {}],
  ]) {
    const p = provider(options);
    assert.notEqual((await handle(request(payload), settings, p.fetch)).status, 200);
    assert.ok(!p.calls.some(c => c.url.includes('smtp2go')));
  }
});

test('accepts the full bounded core order envelope', async () => {
  const longAction = {...action, merchant:'M'.repeat(128), workflow_key:'W'.repeat(128),
    config_ref:'R'.repeat(256), offer_keys:['P000'+'X'.repeat(60)]};
  const manyItems = Array.from({length:100}, (_, i) => [`P${String(i).padStart(3, '0')}${'X'.repeat(60)}`, 1000000000]);
  const payload = {...body, merchant:longAction.merchant, workflow_key:longAction.workflow_key,
    config_ref:longAction.config_ref, config_digest:createHash('sha256').update(JSON.stringify(longAction)).digest('hex'),
    items:manyItems};
  assert.ok(JSON.stringify(payload).length > 8192);
  const p = provider();
  assert.equal((await handle(request(payload), {...env, DELIVERY_CONFIG:JSON.stringify({actions:[longAction]})}, p.fetch)).status, 200);
});

test('two configured workflow instances retain independent identity', async () => {
  const second = {...action, workflow_key:'download-two', config_ref:'private/second'};
  const registry = {...env, DELIVERY_CONFIG:JSON.stringify({actions:[action, second]})};
  const p = provider();
  assert.equal((await handle(request(), registry, p.fetch)).status, 200);
  const payload = {...body, workflow_key:second.workflow_key, config_ref:second.config_ref,
    config_digest:createHash('sha256').update(JSON.stringify(second)).digest('hex')};
  assert.equal((await handle(request(payload), registry, p.fetch)).status, 200);
  assert.equal(p.calls.filter(c => c.url.includes('smtp2go')).length, 2);
});

test('disabled, forged credentials and provider timeout fail closed', async () => {
  let p = provider();
  assert.equal((await handle(request(), {...env, DELIVERY_MODE:'disabled'}, p.fetch)).status, 403);
  assert.equal((await handle(request(body, 'wrong'), env, p.fetch)).status, 403);
  assert.equal(p.calls.length, 0);
  p = provider({failSend:true});
  assert.equal((await handle(request(), env, p.fetch)).status, 503);
  assert.equal(p.calls.filter(c => c.url.includes('smtp2go')).length, 1);
});

test('provider redirects are not followed and cannot trigger a send', async () => {
  let calls = 0;
  const response = await handle(request(), env, async (_url, options) => {
    calls++;
    assert.equal(options.redirect, 'manual');
    return new Response(null, {status:302, headers:{Location:'https://unexpected.example/'}});
  });
  assert.equal(response.status, 403);
  assert.equal(calls, 1);
});
