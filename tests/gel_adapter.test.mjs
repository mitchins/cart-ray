import assert from 'node:assert/strict';
import {mkdtemp,copyFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {pathToFileURL} from 'node:url';
import test from 'node:test';
const dir=await mkdtemp(`${tmpdir()}/cartray-gel-test-`);
await writeFile(`${dir}/package.json`,'{"type":"module"}');
await copyFile(new URL('../examples/gel-theme-pack/src/cartray-adapter.mjs',import.meta.url),`${dir}/adapter.mjs`);
await copyFile(new URL('../storefront/app.js',import.meta.url),`${dir}/core-storefront.js`);
const {createAdapter}=await import(pathToFileURL(`${dir}/adapter.mjs`));
const catalogue={version:'test-v1',products:[{product_key:'ACORN-KIT',title:'Authoritative title',amount_minor:2400,currency:'AUD',max_quantity:3}]};
const presentation={preview:false,gel:{identity:{currency:'USD'}},catalogue:{products:[{product_key:'ACORN-KIT',title:'Fake title',amount_minor:1,slug:'acorn-growing-kit'}]}};
function setup(search='',denied=false){
  const data=new Map();const storage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};
  globalThis.window={CARTRAY_STOREFRONT:{checkoutEnabled:true,apiBaseUrl:'https://api.test.invalid',reviewInvitationsEnabled:true,reviewInvitationDefaultChecked:false},get localStorage(){if(denied)throw Error('denied');return storage;}};
  globalThis.location={search,pathname:'/purchase-return/'};globalThis.history={replaceState(){}};
  globalThis.fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/catalogue')?catalogue:{state:'confirmed'}});
  return {storage,data};
}
test('connected quantity, configured permission and runtime price authority',async()=>{
  setup();const adapter=await createAdapter(presentation);assert.equal(adapter.state.permission,false);assert.equal(adapter.currency,'AUD');
  assert.equal(adapter.products[0].amount_minor,2400);assert.equal(adapter.products[0].title,'Authoritative title');
  adapter.add('ACORN-KIT');adapter.quantity('ACORN-KIT',99);assert.equal(adapter.count(),3);
  assert.equal(adapter.quantity('ACORN-KIT',NaN),false);adapter.quantity('ACORN-KIT',0);assert.equal(adapter.count(),0);
});
test('confirmed return clears matching pending cart only; unknown remains locked',async()=>{
  const {storage}=setup('?checkout=complete&session_id=cs_test_match');
  storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:2,items:[['ACORN-KIT',1]]}));
  storage.setItem('cartray.pending-checkout.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:2,items:[['ACORN-KIT',1]],sessionId:'cs_test_match'}));
  const adapter=await createAdapter(presentation);assert.equal(adapter.quantity('ACORN-KIT',2),false);
  assert.equal((await adapter.returnState()).state,'confirmed');assert.equal(adapter.count(),0);assert.equal(adapter.checkoutLocked,false);
  setup('?checkout=complete&session_id=invalid');const unknown=await createAdapter(presentation);
  assert.equal((await unknown.returnState()).checkoutLocked,true);assert.equal(unknown.add('ACORN-KIT'),false);
});
test('confirmed mismatching cart is retained, expired and cancelled unlock',async()=>{
  const {storage}=setup('?checkout=complete&session_id=cs_test_other');
  storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:2,items:[['ACORN-KIT',2]]}));
  storage.setItem('cartray.pending-checkout.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:1,items:[['ACORN-KIT',1]],sessionId:'cs_test_other'}));
  const changed=await createAdapter(presentation);await changed.returnState();assert.equal(changed.count(),2);
  setup('?checkout=complete&session_id=cs_test_expired');globalThis.fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/catalogue')?catalogue:{state:'expired'}});
  const expired=await createAdapter(presentation);assert.equal((await expired.returnState()).state,'expired');assert.equal(expired.add('ACORN-KIT'),true);
  setup('?checkout=cancelled');const cancelled=await createAdapter(presentation);assert.equal((await cancelled.returnState()).state,'cancelled');assert.equal(cancelled.add('ACORN-KIT'),true);
});
test('connected return displays only durable purchased items with a changed cart or no storage',async()=>{
  for(const denied of [false,true]){
    const {storage}=setup('?checkout=complete&session_id=cs_test_receipt',denied);
    if(!denied)storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:3,items:[['ACORN-KIT',3]]}));
    const receipt={currency:'aud',items:[{product_key:'RETIRED-PURCHASE',quantity:2,unit_amount_minor:1750},
      {product_key:'ACORN-KIT',quantity:1,unit_amount_minor:1750}]};
    globalThis.fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/catalogue')?catalogue:{state:'confirmed',receipt}});
    const adapter=await createAdapter(presentation);const result=await adapter.returnState();
    assert.deepEqual(result.receipt,receipt);assert.match(result.purchasedItems[0],/^Purchased item × 2 /);
    assert.equal(result.purchasedItems.length,2);assert.equal(adapter.count(),denied?0:3);
    assert.match(result.purchasedItems[1],/^Authoritative title × 1 .*17\.50/);
    assert.deepEqual(result.receipt,receipt);
  }
});
test('pending authoritative return retains cart and locks mutation',async()=>{
  const {storage}=setup('?checkout=complete&session_id=cs_test_pending');
  storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:2,items:[['ACORN-KIT',1]]}));
  globalThis.fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/catalogue')?catalogue:{state:'pending'}});
  const timer=globalThis.setTimeout;globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms<30000?0:ms,...args);
  try {const adapter=await createAdapter(presentation);assert.equal((await adapter.returnState()).state,'pending');assert.equal(adapter.count(),1);assert.equal(adapter.checkoutLocked,true);assert.equal(adapter.quantity('ACORN-KIT',0),false);}
  finally {globalThis.setTimeout=timer;}
});
test('storage denial and failed checkout preserve in-memory cart and permit retry',async()=>{
  setup('',true);const adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');
  globalThis.fetch=async()=>{throw Error('offline')};await assert.rejects(adapter.checkout());assert.equal(adapter.count(),1);assert.equal(adapter.checkoutLocked,false);
  let payload;globalThis.fetch=async(_url,request)=>{payload=JSON.parse(request.body);return {ok:true,json:async()=>({session_id:'cs_test_retry',checkout_url:'https://checkout.stripe.com/test'})}};
  assert.equal(await adapter.checkout(),'https://checkout.stripe.com/test');assert.equal(payload.review_invitation_opt_in,false);
});
test.after(async()=>{await rm(dir,{recursive:true,force:true});delete globalThis.window;delete globalThis.fetch;delete globalThis.location;delete globalThis.history;});
test('lost response retries the original request and keeps cart and permission fixed',async()=>{
  const {storage}=setup();const adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');adapter.setPermission(true);
  const requests=[];globalThis.fetch=async(_url,request)=>{requests.push(JSON.parse(request.body));if(requests.length===1)throw Error('lost response');return {ok:true,json:async()=>({session_id:'cs_test_retry',checkout_url:'https://checkout.stripe.com/test'})};};
  await assert.rejects(adapter.checkout());assert.equal(adapter.attemptPending,true);assert.equal(adapter.quantity('ACORN-KIT',2),false);
  adapter.setPermission(false);assert.equal(adapter.state.permission,true);
  assert.equal(await adapter.checkout(),'https://checkout.stripe.com/test');assert.equal(requests[0].checkout_request_id,requests[1].checkout_request_id);
  assert.deepEqual(requests[0],requests[1]);assert.equal(storage.getItem('cartray.checkout-attempt.v1'),null);
});
test('an interrupted attempt survives reload and restores its checkout permission',async()=>{
  const {storage}=setup();let adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');adapter.setPermission(true);
  let original;globalThis.fetch=async(_url,request)=>{original=JSON.parse(request.body);throw Error('lost response');};await assert.rejects(adapter.checkout());
  globalThis.fetch=async(url,request)=>String(url).endsWith('/catalogue')?{ok:true,json:async()=>catalogue}:{ok:true,json:async()=>{assert.deepEqual(JSON.parse(request.body),original);return {session_id:'cs_test_retry',checkout_url:'https://checkout.stripe.com/test'};}};
  adapter=await createAdapter(presentation);assert.equal(adapter.attemptPending,true);assert.equal(adapter.state.permission,true);assert.equal(adapter.add('ACORN-KIT'),false);assert.equal(await adapter.checkout(),'https://checkout.stripe.com/test');assert.equal(storage.getItem('cartray.checkout-attempt.v1'),null);
});
test('a checkout attempt from an old catalogue cannot lock a new cart',async()=>{
  const {storage}=setup();
  storage.setItem('cartray.checkout-attempt.v1',JSON.stringify({requestId:`checkout_${'a'.repeat(32)}`,catalogueVersion:'old',revision:1,items:[['ACORN-KIT',1]],permission:false}));
  const adapter=await createAdapter(presentation);
  assert.equal(adapter.attemptPending,false);assert.equal(adapter.add('ACORN-KIT'),true);
  assert.equal(storage.getItem('cartray.checkout-attempt.v1'),null);
});
test('a rejected checkout can be edited while an in-progress checkout remains retryable',async()=>{
  const {storage}=setup();const adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');
  globalThis.fetch=async()=>({ok:false,status:400});
  await assert.rejects(adapter.checkout());assert.equal(adapter.attemptPending,false);
  assert.equal(adapter.quantity('ACORN-KIT',2),true);assert.equal(storage.getItem('cartray.checkout-attempt.v1'),null);
  globalThis.fetch=async()=>({ok:false,status:409});
  await assert.rejects(adapter.checkout());assert.equal(adapter.attemptPending,true);
  assert.equal(adapter.quantity('ACORN-KIT',1),false);
});

test('retry pins disclosure version across a changed merchant setting',async()=>{
  const {storage}=setup();window.CARTRAY_STOREFRONT.reviewInvitationWordingVersion='review-invitation-v2';
  let adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');adapter.setPermission(true);
  let original;globalThis.fetch=async(_url,request)=>{original=JSON.parse(request.body);throw Error('lost response');};
  await assert.rejects(adapter.checkout());
  assert.equal(JSON.parse(storage.getItem('cartray.checkout-attempt.v1')).wordingVersion,'review-invitation-v2');
  window.CARTRAY_STOREFRONT.reviewInvitationWordingVersion='review-invitation-v1';
  globalThis.fetch=async(url,request)=>String(url).endsWith('/catalogue')?{ok:true,json:async()=>catalogue}:
    {ok:true,json:async()=>{assert.deepEqual(JSON.parse(request.body),original);return {session_id:'cs_test_retry',checkout_url:'https://checkout.stripe.com/test'};}};
  adapter=await createAdapter(presentation);await adapter.checkout();
});

test('pre-upgrade attempt retains the original configured v2 request on retry',async()=>{
  const {storage}=setup();window.CARTRAY_STOREFRONT.reviewInvitationWordingVersion='review-invitation-v2';
  const requestId=`checkout_${'b'.repeat(32)}`;
  storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:1,items:[['ACORN-KIT',1]]}));
  storage.setItem('cartray.checkout-attempt.v1',JSON.stringify({requestId,catalogueVersion:catalogue.version,
    revision:1,items:[['ACORN-KIT',1]],permission:true}));
  const adapter=await createAdapter(presentation);
  globalThis.fetch=async(_url,request)=>{const sent=JSON.parse(request.body);
    assert.equal(sent.checkout_request_id,requestId);assert.equal(sent.review_invitation_wording_version,'review-invitation-v2');
    assert.equal(sent.review_invitation_opt_in,true);
    return {ok:true,json:async()=>({session_id:'cs_test_existing',checkout_url:'https://checkout.stripe.com/test'})};};
  await adapter.checkout();assert.equal(adapter.attemptPending,false);
});

const limited={...presentation,catalogue:{products:[{...presentation.catalogue.products[0],cart_quantity_limit:1}]}};
test('per-product presentation cap narrows quantity without raising server authority',async()=>{
 setup();let adapter=await createAdapter(limited);
 adapter.add('ACORN-KIT');adapter.add('ACORN-KIT');adapter.quantity('ACORN-KIT',99);
 assert.equal(adapter.count(),1);assert.equal(adapter.products[0].max_quantity,1);
 adapter=await createAdapter({...presentation,catalogue:{products:[{...limited.catalogue.products[0],cart_quantity_limit:10}]}});
 adapter.quantity('ACORN-KIT',99);assert.equal(adapter.count(),3);
 for(const cart_quantity_limit of [0,-1,1.5,true,101,null])
  await assert.rejects(createAdapter({...presentation,catalogue:{products:[{...limited.catalogue.products[0],cart_quantity_limit}]}}));
});
test('new cap reconciles editable restored cart but does not rewrite a pending snapshot',async()=>{
 const {storage}=setup();
 storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:7,items:[['ACORN-KIT',2]]}));
 const pending=JSON.stringify({catalogueVersion:catalogue.version,revision:7,items:[['ACORN-KIT',2]],sessionId:'cs_test_original'});
 storage.setItem('cartray.pending-checkout.v1',pending);
 const adapter=await createAdapter(limited);assert.equal(adapter.count(),1);
 assert.equal(JSON.parse(storage.getItem('cartray.cart.v1')).revision,8);
 assert.equal(storage.getItem('cartray.pending-checkout.v1'),pending);
});
test('new display cap cannot reinterpret an uncertain request or its retry',async()=>{
 const {storage}=setup();let adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');adapter.quantity('ACORN-KIT',2);
 let original;globalThis.fetch=async(_url,request)=>{original=JSON.parse(request.body);throw Error('lost response');};
 await assert.rejects(adapter.checkout());const frozen=storage.getItem('cartray.checkout-attempt.v1');
 globalThis.fetch=async(url,request)=>String(url).endsWith('/catalogue')?{ok:true,json:async()=>catalogue}:
  {ok:true,json:async()=>{assert.deepEqual(JSON.parse(request.body),original);return {session_id:'cs_test_retry',checkout_url:'https://checkout.stripe.com/test'};}};
 adapter=await createAdapter(limited);assert.equal(adapter.count(),2);assert.equal(adapter.attemptPending,true);
 assert.equal(adapter.add('ACORN-KIT'),false);assert.equal(adapter.quantity('ACORN-KIT',1),false);
 assert.equal(storage.getItem('cartray.checkout-attempt.v1'),frozen);
 await adapter.checkout();
});
test('new presentation cap leaves the original confirmed-return snapshot match intact',async()=>{
 const {storage}=setup('?checkout=complete&session_id=cs_test_match');
 storage.setItem('cartray.cart.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:7,items:[['ACORN-KIT',2]]}));
 storage.setItem('cartray.pending-checkout.v1',JSON.stringify({catalogueVersion:catalogue.version,revision:7,items:[['ACORN-KIT',2]],sessionId:'cs_test_match'}));
 const adapter=await createAdapter(limited);assert.equal(adapter.count(),2);
 const returned=await adapter.returnState();assert.equal(returned.state,'confirmed');
 assert.match(returned.message,/order is confirmed/);assert.equal(adapter.count(),0);
 assert.equal(storage.getItem('cartray.pending-checkout.v1'),null);
});

test('definitive rejection releases old cap only after preserving the frozen attempt',async()=>{
 setup();let adapter=await createAdapter(presentation);adapter.add('ACORN-KIT');adapter.quantity('ACORN-KIT',2);
 globalThis.fetch=async()=>{throw Error('lost response');};await assert.rejects(adapter.checkout());
 const requests=[];
 globalThis.fetch=async(url,request)=>String(url).endsWith('/catalogue')?{ok:true,json:async()=>catalogue}:
  (requests.push(JSON.parse(request.body)),requests.length===1?{ok:false,status:400}:
   {ok:true,json:async()=>({session_id:'cs_test_new',checkout_url:'https://checkout.stripe.com/test'})});
 adapter=await createAdapter(limited);await assert.rejects(adapter.checkout());
 assert.equal(requests[0].items[0].quantity,2);assert.equal(adapter.count(),1);assert.equal(adapter.attemptPending,false);
 await adapter.checkout();assert.equal(requests[1].items[0].quantity,1);
 assert.notEqual(requests[0].checkout_request_id,requests[1].checkout_request_id);
});

test('legacy theme preview maximum does not silently narrow connected commerce',async()=>{
 setup();const adapter=await createAdapter({...presentation,catalogue:{products:[{...presentation.catalogue.products[0],max_quantity:1}]}});
 adapter.add('ACORN-KIT');adapter.quantity('ACORN-KIT',3);assert.equal(adapter.count(),3);
});

test('return cap reconciliation preserves a newer tab cart and avoids unchanged writes',async()=>{
 for(const quantity of [1,2]){
  const {storage}=setup('?checkout=complete&session_id=cs_test_match');
  const initial={catalogueVersion:catalogue.version,revision:7,items:[['ACORN-KIT',2]]};
  storage.setItem('cartray.cart.v1',JSON.stringify(initial));
  storage.setItem('cartray.pending-checkout.v1',JSON.stringify({...initial,sessionId:'cs_test_match'}));
  const withOther={...catalogue,products:[...catalogue.products,{...catalogue.products[0],product_key:'SECOND',title:'Other product'}]};
  let newer;
  globalThis.fetch=async url=>({ok:true,json:async()=>{
   if(String(url).endsWith('/catalogue'))return withOther;
   newer=JSON.stringify({catalogueVersion:catalogue.version,revision:9,items:[['ACORN-KIT',quantity],['SECOND',1]]},null,2);
   storage.setItem('cartray.cart.v1',newer);
   return {state:'confirmed'};
  }});
  const adapter=await createAdapter(limited);await adapter.returnState();
  assert.deepEqual(adapter.getCart().map(p=>[p.product_key,p.quantity]),[['ACORN-KIT',1],['SECOND',1]]);
  const saved=JSON.parse(storage.getItem('cartray.cart.v1'));
  assert.deepEqual(saved.items,[['ACORN-KIT',1],['SECOND',1]]);
  assert.equal(saved.revision,quantity===1?9:10);
  if(quantity===1)assert.equal(storage.getItem('cartray.cart.v1'),newer);
 }
});
