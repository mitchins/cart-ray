import test from 'node:test';
import assert from 'node:assert/strict';
import {handle} from '../modules/post-purchase/transactional-email/worker.mjs';

const env = {DELIVERY_MODE:'test', ENVIRONMENT:'test', HANDLER_TOKEN:'a'.repeat(32),
  MERCHANT_ID:'merchant-one', MERCHANT_NAME:'Example Store', STOREFRONT_ORIGIN:'https://shop.example',
  APPROVED_TEST_RECIPIENT:'recipient@example.test', APPROVED_TEST_SENDER:'sender@example.test',
  STRIPE_ACCOUNT_ID:'acct_test', STRIPE_API_KEY:'sk_test_example', SMTP2GO_API_KEY:'api-example'};
const body = {type:'order_confirmation', merchant:'merchant-one', environment:'test', order_id:'order-one',
  session_id:'cs_test_123', access_url:'https://shop.example/order-access#token='+'a'.repeat(43),
  items:[{product_key:'one',title:'First product',quantity:2},{product_key:'two',title:'Second product',quantity:1}]};
function request(value=body, token=env.HANDLER_TOKEN) {
  return new Request('https://post-purchase-handler.internal/', {method:'POST',
    headers:{authorization:`Bearer ${token}`}, body:JSON.stringify(value)});
}
function provider(options={}) {
  const calls=[];
  return {calls, fetch:async (url, init={}) => {
    calls.push({url,init});
    if (url.endsWith('/v1/account')) return Response.json({id:options.account || 'acct_test'});
    if (url.includes('/v1/checkout/sessions/')) return Response.json({id:'cs_test_123',livemode:false,
      mode:'payment',status:'complete',payment_status:options.payment || 'paid',amount_total:options.amount ?? 12500,currency:options.currency || 'aud',
      metadata:{cr_order_id:options.order || 'order-one'},customer_details:{email:options.email || 'recipient@example.test'}});
    if (options.failSend) throw new Error('provider timeout');
    if (url.includes('api.cloudflare.com')) return Response.json(options.receipt);
    return Response.json({data:{succeeded:1,failed:0,email_id:'receipt-one'}});
  }};
}

test('one confirmation includes every item, quantity and the accountless order link', async () => {
  const p=provider(); const result=await handle(request(),env,p.fetch);
  assert.equal(result.status,200);
  assert.deepEqual(await result.json(),{provider_reference:'receipt-one'});
  assert.equal(p.calls.filter(c=>c.url.includes('/email/send')).length,1);
  const message=JSON.parse(p.calls.at(-1).init.body);
  assert.match(message.text_body,/First product × 2/);
  assert.match(message.text_body,/Second product × 1/);
  assert.ok(message.text_body.includes(body.access_url));
  assert.match(message.text_body,/Example Store/);
});

test('unavailable order access does not prevent the receipt', async () => {
  const p=provider(); const result=await handle(request({...body,access_url:null}),env,p.fetch);
  assert.equal(result.status,200);
  assert.match(JSON.parse(p.calls.at(-1).init.body).text_body,/available when ready/);
  assert.equal(p.calls.filter(c=>c.url.includes('/email/send')).length,1);
});

test('forged scope, unconfirmed purchase or untrusted recipient cannot send', async () => {
  for (const [value,settings,options] of [
    [{...body,merchant:'other'},env,{}],
    [{...body,access_url:'https://other.example/order-access#token='+'a'.repeat(43)},env,{}],
    [{...body,access_url:'https://shop.example/order-access?token=secret'},env,{}],
    [{...body,items:[{product_key:'one',title:'Example',quantity:0}]},env,{}],
    [body,{...env,DELIVERY_MODE:'disabled'},{}],
    [body,env,{account:'acct_other'}], [body,env,{payment:'unpaid'}],
    [body,env,{order:'other-order'}], [body,env,{email:'other@example.test'}],
  ]) {
    const p=provider(options); const response=await handle(request(value),settings,p.fetch);
    assert.notEqual(response.status,200);
    assert.equal(p.calls.filter(c=>c.url.includes('/email/send')).length,0);
  }
});

test('Cloudflare acceptance and uncertain provider failure do not fall back or retry', async () => {
  const cloud={...env,EMAIL_PROVIDER:'cloudflare',CLOUDFLARE_EMAIL_ACCOUNT_ID:'a'.repeat(32),
    CLOUDFLARE_EMAIL_API_TOKEN:'x'.repeat(40),SMTP2GO_API_KEY:undefined};
  const receipt={success:true,errors:[],result:{message_id:'<receipt@example.test>',delivered:[],
    queued:['recipient@example.test'],permanent_bounces:[],suppressed_recipients:[]}};
  let p=provider({receipt}); let response=await handle(request(),cloud,p.fetch);
  assert.equal(response.status,200);
  assert.deepEqual(await response.json(),{provider_reference:'<receipt@example.test>'});
  p=provider({failSend:true}); response=await handle(request(),cloud,p.fetch);
  assert.notEqual(response.status,200);
  assert.equal(p.calls.filter(c=>c.url.includes('/email/sending/send')).length,1);
  assert.equal(p.calls.filter(c=>c.url.includes('smtp2go')).length,0);
});


test('zero-decimal currency total is not divided by one hundred', async () => {
  const p=provider({currency:'jpy',amount:500});
  assert.equal((await handle(request(),env,p.fetch)).status,200);
  assert.match(JSON.parse(p.calls.at(-1).init.body).text_body,/500/);
});

test('Stripe special-case charge units use hundredths for ISK and UGX', async () => {
  for (const currency of ['isk', 'ugx']) {
    const p=provider({currency,amount:500});
    assert.equal((await handle(request(),env,p.fetch)).status,200);
    const total=JSON.parse(p.calls.at(-1).init.body).text_body.split('\n').find(line=>line.startsWith('Total:'));
    assert.match(total,/5/);
    assert.doesNotMatch(total,/500/);
  }
});

test('merchant name bound counts Unicode code points', async () => {
  const p=provider();
  assert.equal((await handle(request(),{...env,MERCHANT_NAME:'🌲'.repeat(51)},p.fetch)).status,200);
});

test('confirmed free orders receive a receipt, but unpaid orders do not', async () => {
  let p=provider({amount:0,payment:'no_payment_required'});
  assert.equal((await handle(request(),env,p.fetch)).status,200);
  p=provider({amount:100,payment:'no_payment_required'});
  assert.notEqual((await handle(request(),env,p.fetch)).status,200);
  assert.equal(p.calls.filter(c=>c.url.includes('/email/send')).length,0);
});

test('a full checkout with unicode titles fits the receipt', async () => {
  const items=Array.from({length:100},(_,i)=>({product_key:`item-${i}`,title:'é'.repeat(190),quantity:1}));
  const p=provider();
  assert.equal((await handle(request({...body,items}),env,p.fetch)).status,200);
  assert.match(JSON.parse(p.calls.at(-1).init.body).text_body,/item|é/);
});
