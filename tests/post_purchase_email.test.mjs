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
    if (url.includes('/v1/checkout/sessions/')) return Response.json({id:options.session || 'cs_test_123',livemode:!!options.live,
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

test('merchant sender and authoritative Stripe recipient work outside the sandbox restriction', async () => {
  const p=provider({email:'buyer@example.test',live:true,session:'cs_live_123'});
  const settings={...env,ENVIRONMENT:'live',DELIVERY_MODE:'live',STRIPE_API_KEY:'sk_live_synthetic',
    MERCHANT_SENDER:'orders@example.test',APPROVED_TEST_SENDER:undefined,APPROVED_TEST_RECIPIENT:undefined};
  assert.equal((await handle(request({...body,environment:'live',session_id:'cs_live_123'}),settings,p.fetch)).status,200);
  const message=JSON.parse(p.calls.at(-1).init.body);
  assert.equal(message.sender,'orders@example.test');
  assert.deepEqual(message.to,['buyer@example.test']);
});

test('sandbox sending requires the approved test recipient', async () => {
  const p=provider();
  assert.notEqual((await handle(request(),{...env,APPROVED_TEST_RECIPIENT:undefined},p.fetch)).status,200);
  assert.equal(p.calls.length,0);
});

test('sender selection is environment-scoped and test delivery uses the approved address', async () => {
  let p=provider({email:'recipient@example.test'});
  const settings={...env,APPROVED_TEST_RECIPIENT:'Recipient@example.test',MERCHANT_SENDER:'other@example.test'};
  assert.equal((await handle(request(),settings,p.fetch)).status,200);
  const message=JSON.parse(p.calls.at(-1).init.body);
  assert.equal(message.sender,env.APPROVED_TEST_SENDER);
  assert.deepEqual(message.to,['Recipient@example.test']);
  p=provider({live:true,session:'cs_live_123'});
  const live={...env,ENVIRONMENT:'live',DELIVERY_MODE:'live',STRIPE_API_KEY:'sk_live_synthetic'};
  assert.notEqual((await handle(request({...body,environment:'live',session_id:'cs_live_123'}),live,p.fetch)).status,200);
  assert.equal(p.calls.length,0);
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

test('Stripe two-decimal charges retain fractions when locale defaults omit them', async () => {
  for (const currency of ['huf','idr','cop']) {
    const p=provider({currency,amount:1045});
    assert.equal((await handle(request(),env,p.fetch)).status,200);
    assert.match(JSON.parse(p.calls.at(-1).init.body).text_body,/10\.45/);
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

test('receipt, followup and recipient evidence use the same verified provider boundary', async () => {
  const preferences={...env,MERCHANT_CONTACT:'help@example.test'};
  const reviews={url:'https://shop.example/review/#invitation='+'a'.repeat(64)+'&featured=second',
    unsubscribe_url:'https://shop.example/review-unsubscribe?token='+'b'.repeat(64), subjects:['second'],names:{second:'Second product'}};
  const p=provider({email:'Recipient@example.test'});
  const evidence=await handle(request({...body,type:'recipient_evidence'}),preferences,p.fetch);
  assert.deepEqual(await evidence.json(),{email:env.APPROVED_TEST_RECIPIENT});
  assert.equal(p.calls.filter(c=>c.url.includes('/email/send')).length,0);
  for(const type of ['order_confirmation','review_followup']) {
    const result=await handle(request({...body,type,reviews}),preferences,p.fetch);
    assert.equal(result.status,200);
    const message=JSON.parse(p.calls.at(-1).init.body);
    assert.ok(message.text_body.includes(reviews.url));
    assert.match(message.text_body,/Second product/);
    assert.equal(message.custom_headers.find(h=>h.header==='List-Unsubscribe-Post').value,'List-Unsubscribe=One-Click');
    assert.equal(message.to[0],env.APPROVED_TEST_RECIPIENT);
  }
});

test('invalid optional review data never blocks the payment receipt', async () => {
  const p=provider();
  assert.equal((await handle(request({...body,reviews:{url:'https://evil.example'}}),env,p.fetch)).status,200);
  assert.match(JSON.parse(p.calls.at(-1).init.body).text_body,/Your purchased|View your purchased/);
  assert.equal((await handle(request({...body,type:'review_followup',reviews:{}}),env,p.fetch)).status,400);
});

test('account lookup failures are retryable while confirmed recipient rejection is specific',async()=>{
  for(const status of [401,429,500,503]) {
    const response=await handle(request({...body,type:'recipient_evidence'}),env,async()=>new Response('',{status}));
    assert.equal(response.status,502);
  }
  let p=provider({account:'acct_wrong'});
  assert.equal((await handle(request({...body,type:'recipient_evidence'}),env,p.fetch)).status,403);
  p=provider({email:'unapproved@example.test'});
  assert.equal((await handle(request({...body,type:'recipient_evidence'}),env,p.fetch)).status,422);
});


test('paused provider verifies a purchase recipient without admitting delivery',async()=>{
  const paused={...env,DELIVERY_MODE:'disabled'};
  const p=provider();
  const evidence=await handle(request({...body,type:'recipient_evidence'}),paused,p.fetch);
  assert.equal(evidence.status,200);
  assert.deepEqual(await evidence.json(),{email:env.APPROVED_TEST_RECIPIENT});
  assert.equal(p.calls.length,2);
  for(const type of ['order_confirmation','review_followup'])
    assert.equal((await handle(request({...body,type}),paused,p.fetch)).status,403);
  assert.equal(p.calls.length,2);
  assert.equal((await handle(request({...body,type:'recipient_evidence'},'wrong'),paused,p.fetch)).status,403);
  assert.equal(p.calls.length,2);
});

const allowedTest = {addresses:['specific@another.test'],domains:['effectivepolicy.com.au','mitchellcurrie.com']};
const testSettings = policy => ({...env,DELIVERY_CONFIG:JSON.stringify({test_recipient_allowlist:policy})});
const reviewContent={url:'https://shop.example/review/#invitation='+'a'.repeat(64),
  unsubscribe_url:'https://shop.example/review-unsubscribe?token='+'b'.repeat(64),subjects:['one']};
for (const address of ['qualityandcompliance@effectivepolicy.com.au','test@mitchellcurrie.com',
  'QualityAndCompliance@EffectivePolicy.COM.AU','specific@another.test']) {
  test(`test allowlist uses authoritative purchaser for all message purposes: ${address}`,async()=>{
    for(const purpose of ['recipient_evidence','order_confirmation','review_followup','recovery']) {
      const type=purpose==='recovery'?'review_followup':purpose;
      const p=provider({email:address});
      const result=await handle(request({...body,type,reviews:reviewContent,recipient:'attacker@outside.test',
        ...(purpose==='recovery'?{delivery_identity:'d'.repeat(64)}:{})}),
        {...testSettings(allowedTest),MERCHANT_CONTACT:'help@example.test'},p.fetch);
      assert.equal(result.status,200);
      if(type==='recipient_evidence') {
        assert.deepEqual(await result.json(),{email:address});assert.equal(p.calls.length,2);
      } else assert.deepEqual(JSON.parse(p.calls.at(-1).init.body).to,[address]);
    }
  });
}
for (const address of ['buyer@outside.test','buyer@evilmitchellcurrie.com','buyer@sub.mitchellcurrie.com',
  'buyer@mitchellcurrie.com.evil.test','buyer@effectivеpolicy.com.au','buyer@effectivepolicy.com.au.',
  'specific+extra@another.test']) {
  test(`outside, subdomain, suffix and lookalike recipients rejected: ${address}`,async()=>{
    const p=provider({email:address});
    const result=await handle(request({...body,recipient:'test@mitchellcurrie.com'}),testSettings(allowedTest),p.fetch);
    assert.equal(result.status,422);assert.equal(p.calls.length,2);
  });
}
for (const policy of [null,{},[],{addresses:[],domains:[]},{addresses:[],domains:['*.example.test']},
  {addresses:[],domains:['https://example.test']},{addresses:[],domains:['example.test','EXAMPLE.TEST']},
  {addresses:['bad\r\n@example.test'],domains:[]},{addresses:[],domains:['еxample.test']},
  {addresses:[],domains:Array.from({length:33},(_,i)=>`d${i}.test`)},
  {addresses:[],domains:['example.test'],extra:true}]) {
  test(`malformed test allowlist fails before Stripe: ${JSON.stringify(policy)}`,async()=>{
    const p=provider(); assert.notEqual((await handle(request(),testSettings(policy),p.fetch)).status,200);
    assert.equal(p.calls.length,0);
  });
}
test('live delivery ignores the sandbox allowlist; test OFF cannot send to newly approved purchasers',async()=>{
 const p=provider({email:'buyer@outside.test',live:true,session:'cs_live_123'});
 const live={...testSettings(null),ENVIRONMENT:'live',DELIVERY_MODE:'live',STRIPE_API_KEY:'sk_live_synthetic',MERCHANT_SENDER:'orders@example.test'};
 assert.equal((await handle(request({...body,environment:'live',session_id:'cs_live_123'}),live,p.fetch)).status,200);
 const off=provider({email:'test@mitchellcurrie.com'});
 assert.equal((await handle(request(),{...testSettings(allowedTest),DELIVERY_MODE:'disabled'},off.fetch)).status,403);
 assert.equal(off.calls.length,0);
});

test('non-ASCII purchaser lookalikes cannot fold into exact address or domain permissions',async()=>{
 for(const [address,policy] of [
   ['Kate@example.test',{addresses:['kate@example.test'],domains:[]}],
   ['buyer@Kate.test',{addresses:[],domains:['kate.test']}],
   ['Kate@mitchellcurrie.com',allowedTest]]) {
   const p=provider({email:address});
   assert.equal((await handle(request(),testSettings(policy),p.fetch)).status,422);
   assert.equal(p.calls.length,2);
 }
});
test('oversized runtime delivery policy fails before any provider call',async()=>{
 const p=provider();
 assert.notEqual((await handle(request(),{...env,DELIVERY_CONFIG:' '.repeat(4097)+'{}'},p.fetch)).status,200);
 assert.equal(p.calls.length,0);
});


test('legacy mixed-case recipient evidence is retained with and without an allowlist',async()=>{
 for(const policy of [undefined,{addresses:[],domains:['example.test']}]) {
   const settings={...env,APPROVED_TEST_RECIPIENT:'Mitch@example.test',
     ...(policy?{DELIVERY_CONFIG:JSON.stringify({test_recipient_allowlist:policy})}:{})};
   for(const type of ['recipient_evidence','order_confirmation']) {
     const p=provider({email:'mitch@EXAMPLE.TEST'});
     const response=await handle(request({...body,type}),settings,p.fetch);
     assert.equal(response.status,200);
     if(type==='recipient_evidence')assert.deepEqual(await response.json(),{email:'Mitch@example.test'});
     else assert.deepEqual(JSON.parse(p.calls.at(-1).init.body).to,['Mitch@example.test']);
   }
 }
});


test('legacy quoted mailbox remains supported without widening explicit allowlists',async()=>{
 const address='"Buyer"@example.test';
 const settings={...env,APPROVED_TEST_RECIPIENT:address};
 const p=provider({email:'"buyer"@EXAMPLE.TEST'});
 const response=await handle(request({...body,type:'recipient_evidence'}),settings,p.fetch);
 assert.equal(response.status,200);assert.deepEqual(await response.json(),{email:address});
 const strict=provider({email:address});
 assert.equal((await handle(request(),{...settings,...testSettings({addresses:[],domains:['example.test']})},strict.fetch)).status,422);
});
test('legacy fallback rejects Unicode before case conversion',async()=>{
 const p=provider({email:'Kate@example.test'});
 assert.equal((await handle(request(),{...env,APPROVED_TEST_RECIPIENT:'kate@example.test'},p.fetch)).status,422);
 assert.equal(p.calls.length,2);
});

test('one hundred purchased products plus store fit both consolidated message types',async()=>{
  const subjects=[...Array.from({length:100},(_,i)=>`product-${i}`),'merchant-store'];
  const reviews={url:'https://shop.example/review/#invitation='+'a'.repeat(64),
    unsubscribe_url:'https://shop.example/review-unsubscribe?token='+'b'.repeat(64),subjects};
  for (const type of ['order_confirmation','review_followup']) {
    const p=provider();
    const response=await handle(request({...body,type,reviews}),{...env,MERCHANT_CONTACT:'help@example.test'},p.fetch);
    assert.equal(response.status,200);
    const sent=JSON.parse(p.calls.at(-1).init.body);
    assert.ok(sent.text_body.includes(reviews.url));
    assert.match(sent.text_body,/merchant-store/);
    assert.match(sent.text_body,/Review choices:/);
    assert.equal(p.calls.filter(call=>call.url.includes('/email/send')).length,1);
  }
});
