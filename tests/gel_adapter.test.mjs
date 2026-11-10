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
