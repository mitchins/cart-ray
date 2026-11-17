/** Execute the test-only handler in Workerd with synthetic outbound providers. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {Miniflare} from 'miniflare';

test('private registered email handler runs in Workerd with synthetic providers', async () => {
  const action = {environment:'test', merchant:'merchant-one', workflow_key:'download-one',
    config_ref:'private/download', offer_keys:['product-one'], links:['https://www.dropbox.com/t/example'],
    recipient:'recipient@example.test', sender:'sender@example.test', subject:'Test delivery', stripe_account:'acct_test'};
  const body = {merchant:'merchant-one', environment:'test', workflow_key:'download-one', config_ref:'private/download',
    config_digest:createHash('sha256').update(JSON.stringify(action)).digest('hex'), order_id:'order-one',
    session_id:'cs_test_123', items:[['product-one',1]]};
  const calls=[];
  const outboundService={type:'fetcher',handler:async request => {
    const url=new URL(request.url); calls.push(url.hostname + url.pathname);
    if (url.pathname === '/v1/account') return Response.json({id:'acct_test'});
    if (url.pathname.startsWith('/v1/checkout/sessions/')) return Response.json({id:'cs_test_123',livemode:false,
      mode:'payment',status:'complete',payment_status:'paid',metadata:{cr_order_id:'order-one'},
      customer_details:{email:'recipient@example.test'}});
    assert.equal(url.hostname,'api.smtp2go.com');
    assert.deepEqual((await request.json()).to,['recipient@example.test']);
    return Response.json({data:{succeeded:1,failed:0,email_id:'test-receipt'}});
  }};
  const script=readFileSync(new URL('../modules/post-purchase/transactional-email/worker.mjs',import.meta.url),'utf8');
  const values={DELIVERY_MODE:'test',ENVIRONMENT:'test',HANDLER_TOKEN:'x'.repeat(40),
    DELIVERY_CONFIG:JSON.stringify({actions:[action]}),APPROVED_TEST_RECIPIENT:'recipient@example.test',
    APPROVED_TEST_SENDER:'sender@example.test',STRIPE_ACCOUNT_ID:'acct_test',
    STRIPE_API_KEY:'sk_test_synthetic',SMTP2GO_API_KEY:'api-synthetic'};
  const env=Object.fromEntries(Object.entries(values).map(([key,value])=>[key,{type:'json',value}]));
  const mf=new Miniflare({workers:[{config:{name:'post-purchase-email',type:'worker',compatibilityDate:'2026-08-31',
    manifest:{mainModule:'worker.mjs',modules:{'worker.mjs':{type:'esm',contents:script}}},env},dev:{outboundService}}]});
  try {
    const response=await mf.dispatchFetch('https://private.invalid/',{method:'POST',
      headers:{Authorization:'Bearer '+'x'.repeat(40)},body:JSON.stringify(body)});
    assert.equal(response.status,200,`outbound calls: ${JSON.stringify(calls)}`);
    assert.deepEqual(await response.json(),{provider_reference:'test-receipt'});
    assert.equal(calls.length,3);
  } finally { await mf.dispose(); }
});
