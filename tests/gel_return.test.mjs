import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import {confirmedCheckoutView} from '../storefront/app.js';

// Isolate the shared return renderer without booting catalogue, cart or review flows.
const source=await readFile(new URL('../examples/gel-theme-pack/src/app.mjs',import.meta.url),'utf8');
const render=source.slice(source.indexOf('async function renderReturn('),source.indexOf('function renderInbox('));

test('GEL shared renderer shows confirmed receipt lines as text, never current cart contents',async()=>{
  const result={state:'confirmed',...confirmedCheckoutView(false,{currency:'aud',items:[
    {product_key:'<img src=x onerror=alert(1)>',quantity:2,unit_amount_minor:1250},
    {product_key:'RETIRED-PRODUCT',quantity:1,unit_amount_minor:0},
  ]},[{product_key:'RETIRED-PRODUCT',title:'<script>alert("label")</script>'}])};
  assert.match(result.purchasedItems[1],/^<script>alert\("label"\)<\/script> \(RETIRED-PRODUCT\)/);
  for(const state of ['confirmed','pending','expired','unavailable']){
    const lists=[];const content={innerHTML:''};const heading={textContent:''};
    const context=vm.createContext({
      adapter:{preview:false,getCart(){throw Error('Receipt must not read cart');}},
      location:{pathname:'/purchase-return/',search:''},
      document:{dispatchEvent(){},createElement(tag){return {tag,children:[],setAttribute(){},append(child){this.children.push(child);}};}},
      CustomEvent:class {},icon:()=>'',h:String,url:String,
      $:selector=>selector==='#content'?content:selector.includes('button-row')?{before:list=>lists.push(list)}:heading,
    });
    await vm.runInContext(`${render}; renderReturn(${JSON.stringify({...result,state})})`,context);
    assert.equal(lists.length,state==='confirmed'?1:0);
    if(state==='confirmed'){
      assert.equal(lists[0].children.length,2);
      assert.equal(lists[0].children[0].textContent,result.purchasedItems[0]);
      assert.equal(lists[0].children[1].textContent,result.purchasedItems[1]);
      assert.ok(lists[0].children.every(line=>line.innerHTML===undefined));
    }
  }
});

test('bare presenter inserts malicious catalogue labels and missing keys only as text',async()=>{
  const bare=await readFile(new URL('../storefront/app.js',import.meta.url),'utf8');
  const block=bare.slice(bare.indexOf('      if (view.purchasedItems)'),bare.indexOf('    } else if (result.state === "expired")'));
  const view=confirmedCheckoutView(false,{currency:'aud',items:[
    {product_key:'KNOWN',quantity:2,unit_amount_minor:1250},
    {product_key:'MISSING',quantity:1,unit_amount_minor:0},
  ]},[{product_key:'KNOWN',title:'<img src=x onerror=alert(1)>'}]);
  const lists=[];
  vm.runInNewContext(block,{
    view,status:{after:list=>lists.push(list)},
    document:{createElement(tag){return {tag,children:[],setAttribute(){},append(child){this.children.push(child);}};}},
  });
  assert.equal(lists.length,1);
  assert.equal(lists[0].children[0].textContent,view.purchasedItems[0]);
  assert.match(lists[0].children[0].textContent,/^<img src=x onerror=alert\(1\)> \(KNOWN\) × 2 /);
  assert.match(lists[0].children[1].textContent,/^MISSING × 1 /);
  assert.ok(lists[0].children.every(line=>line.innerHTML===undefined));
});
