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
    if (url.includes('api.cloudflare.com')) return Response.json(options.cloudflareResponse, {status:options.sendStatus || 200});
    return Response.json({data:{succeeded:1, failed:0, email_id:'receipt-one'}});
  }};
}

test('confirmed approved test purchase delivers configured dummy links', async () => {
  const p = provider();
  const result = await handle(request(), env, p.fetch);
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), {provider_reference:'receipt-one'});
  assert.equal(p.calls.length, 3);
  assert.ok(p.calls[0].init.signal instanceof AbortSignal);
  assert.equal(p.calls[0].init.signal, p.calls[1].init.signal);
  assert.equal(p.calls[1].init.signal, p.calls[2].init.signal);
  assert.deepEqual(JSON.parse(p.calls[2].init.body).to, ['recipient@example.test']);
});

const cloudflareEnv = {...env, EMAIL_PROVIDER:'cloudflare', CLOUDFLARE_EMAIL_ACCOUNT_ID:'a'.repeat(32),
  CLOUDFLARE_EMAIL_API_TOKEN:'v1.0-fixture-'.repeat(4), SMTP2GO_API_KEY:undefined};
const cloudflareReceipt = {success:true, errors:[], result:{message_id:'<receipt@example.test>',
  delivered:['recipient@example.test'], queued:[], permanent_bounces:[], suppressed_recipients:[]}};

test('Cloudflare accepts exactly one delivered OR queued recipient and retains the shared deadline', async () => {
  for (const queued of [false,true]) {
    const receipt=structuredClone(cloudflareReceipt);
    if (queued) {receipt.result.queued=receipt.result.delivered;receipt.result.delivered=[];}
    const p=provider({cloudflareResponse:receipt});
    const response=await handle(request(),cloudflareEnv,p.fetch);
    assert.equal(response.status,200);
    assert.deepEqual(await response.json(),{provider_reference:'<receipt@example.test>'});
    assert.equal(p.calls.length,3);
    const last=p.calls[2];
    assert.equal(last.url,`https://api.cloudflare.com/client/v4/accounts/${'a'.repeat(32)}/email/sending/send`);
    assert.equal(last.init.headers.Authorization,`Bearer ${cloudflareEnv.CLOUDFLARE_EMAIL_API_TOKEN}`);
    assert.deepEqual(JSON.parse(last.init.body),{from:action.sender,to:action.recipient,subject:action.subject,
      text:'This test purchase contains dummy materials only.\n\n'+action.links.join('\n')});
    assert.ok(p.calls.every(c=>c.init.redirect==='manual' && c.init.signal===last.init.signal));
    assert.equal(last.init.headers['X-Smtp2go-Api-Key'],undefined);
  }
});

test('Cloudflare bounce, suppression, extra/duplicate recipients and ambiguous receipts never succeed or retry', async () => {
  const changes=[
    {message_id:undefined},{message_id:123},{message_id:'bad\nreference'},
    {delivered:[],queued:[]},{delivered:['other@example.test']},
    {queued:['recipient@example.test']},{delivered:['recipient@example.test','other@example.test']},
    {delivered:['recipient@example.test','recipient@example.test']},
    {permanent_bounces:['recipient@example.test']},{suppressed_recipients:['recipient@example.test']},
    {delivered:null},{queued:undefined},{permanent_bounces:undefined},{suppressed_recipients:'invalid'},
    {suppressed_recipients:undefined},
  ];
  const receipts=changes.map(change=>({...cloudflareReceipt,result:{...cloudflareReceipt.result,...change}}));
  receipts.push({...cloudflareReceipt,success:false},{...cloudflareReceipt,errors:[{code:10001}]},
    {...cloudflareReceipt,result:null},{...cloudflareReceipt,errors:undefined});
  for (const receipt of receipts) {
    const p=provider({cloudflareResponse:receipt});
    assert.equal((await handle(request(),cloudflareEnv,p.fetch)).status,502);
    assert.equal(p.calls.length,3);
  }
  for (const options of [{failSend:true},{cloudflareResponse:cloudflareReceipt,sendStatus:302},
    {cloudflareResponse:cloudflareReceipt,sendStatus:403}]) {
    const p=provider(options);
    assert.notEqual((await handle(request(),cloudflareEnv,p.fetch)).status,200);
    assert.equal(p.calls.length,3);
  }
});

test('Cloudflare retains all test scope, receipt digest and recipient/Stripe guards; no fallback provider', async () => {
  for (const [payload,settings,options] of [
    [{...body,config_digest:'0'.repeat(64)},cloudflareEnv,{}],
    [body,{...cloudflareEnv,APPROVED_TEST_RECIPIENT:'other@example.test'},{}],
    [body,{...cloudflareEnv,APPROVED_TEST_SENDER:'other@example.test'},{}],
    [body,{...cloudflareEnv,STRIPE_API_KEY:'sk_live_bad'},{}],
    [body,{...cloudflareEnv,DELIVERY_MODE:'disabled'},{}],
    [body,{...cloudflareEnv,EMAIL_PROVIDER:'unknown',SMTP2GO_API_KEY:'api-fixture'},{}],
    [body,{...cloudflareEnv,CLOUDFLARE_EMAIL_ACCOUNT_ID:'../other'},{}],
    [body,{...cloudflareEnv,CLOUDFLARE_EMAIL_API_TOKEN:'bad\nheader'},{}],
    [body,cloudflareEnv,{account:'acct_other'}],
    [body,cloudflareEnv,{status:'unpaid'}],
    [body,cloudflareEnv,{order:'other-order'}],
    [body,cloudflareEnv,{recipient:'other@example.test'}],
    [{...body,items:[['other',1]]},cloudflareEnv,{}],
    [{...body,session_id:'cs_live_123'},cloudflareEnv,{}],
  ]) {
    const p=provider(options);
    assert.notEqual((await handle(request(payload),settings,p.fetch)).status,200);
    assert.ok(!p.calls.some(c=>c.url.includes('cloudflare') || c.url.includes('smtp2go')));
  }
  const unsafeAction={...action,links:['https://example.test/not-dummy']};
  const payload={...body,config_digest:createHash('sha256').update(JSON.stringify(unsafeAction)).digest('hex')};
  const p=provider();
  assert.equal((await handle(request(payload),{...cloudflareEnv,DELIVERY_CONFIG:JSON.stringify({actions:[unsafeAction]})},p.fetch)).status,400);
  assert.equal(p.calls.length,0);
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
