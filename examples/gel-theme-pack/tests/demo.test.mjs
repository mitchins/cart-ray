import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {newDemoStore,cleanCart} from '../src/demo-adapter.mjs';
const gel=JSON.parse(fs.readFileSync(new URL('../gel/squirrel.json',import.meta.url)));
const catalogue=JSON.parse(fs.readFileSync(new URL('../content/squirrel.json',import.meta.url)));
function setup(){const values=new Map();const storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};return {storage,config:{preview:true,gel,catalogue,base:'/minimal/'},values};}
test('demo requires explicit preview mode',()=>{const f=setup();assert.throws(()=>newDemoStore({...f.config,preview:false},f.storage));});
test('cart accepts only known integer quantities and clamps maximum',()=>{assert.deepEqual(cleanCart({'ACORN-KIT':99,'ACORN-PLANTER':-1,unknown:5},catalogue.products),{'ACORN-KIT':10});assert.deepEqual(cleanCart([],catalogue.products),{});});
test('cart mutations survive a new presenter and never use actual CartRay keys',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.add('ACORN-KIT');s.add('ACORN-KIT');assert.equal(s.count(),2);assert.equal(s.subtotal(),4800);assert.equal(newDemoStore(f.config,f.storage).count(),2);assert.ok([...f.values.keys()].every(k=>k.startsWith('cartray.theme-demo.')));});
test('quantity zero removes, negatives and unknown products do not mutate',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.add('ACORN-KIT');assert.equal(s.quantity('ACORN-KIT',-1),false);assert.equal(s.add('unknown'),false);s.quantity('ACORN-KIT',0);assert.equal(s.count(),0);});
test('checkout acceptance is just a preview route, never a payment request',async()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.add('ACORN-KIT');assert.equal(await s.checkout(),'/minimal/checkout-preview/');assert.equal(s.state.order,null);});
test('opt out before purchase produces no invitation',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.add('ACORN-KIT');s.setPermission(false);s.simulatePayment();assert.equal(s.state.order.invitation,false);});
test('checked purchase creates one preview invitation for two products',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.add('ACORN-KIT');s.add('ACORN-PLANTER');s.simulatePayment();assert.equal(s.state.order.invitation,true);assert.equal(s.reviewable().length,2);assert.equal(s.count(),0);});
test('free guide is not reviewable',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.add('ACORN-GUIDE');s.simulatePayment();assert.equal(s.state.order.invitation,false);assert.equal(s.reviewable().length,0);});
test('rating-only and text-only review paths remain available',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.review('ACORN-KIT',{rating:5,body:'',title:'',author:''});s.review('ACORN-PLANTER',{rating:null,body:'Lovely',title:'',author:''});assert.equal(s.state.reviews['ACORN-KIT'].status,'pending');assert.equal(s.state.reviews['ACORN-PLANTER'].rating,null);});
test('empty review is rejected without state changes',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);assert.throws(()=>s.review('ACORN-KIT',{rating:null,body:' '}));assert.equal(s.state.reviews['ACORN-KIT'],undefined);});
test('withdrawal changes only the review',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.review('ACORN-KIT',{rating:4,body:'',title:'',author:''});s.add('ACORN-PLANTER');s.withdraw('ACORN-KIT');assert.equal(s.state.reviews['ACORN-KIT'].status,'withdrawn');assert.equal(s.count(),1);});
test('unsubscribe persists across a later purchase and presenter',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.unsubscribe();const next=newDemoStore(f.config,f.storage);next.add('ACORN-KIT');next.simulatePayment();assert.equal(next.state.order.invitation,false);});
test('storage denial leaves preview usable in memory',()=>{const f=setup();const s=newDemoStore(f.config,{getItem(){throw Error()},setItem(){throw Error()}});s.add('ACORN-KIT');assert.equal(s.count(),1);s.simulatePayment();assert.ok(s.state.order);});
test('locked cart rejects edits',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.checkoutLocked=true;assert.equal(s.add('ACORN-KIT'),false);assert.equal(s.count(),0);});

test('withdrawal does not create a new review entitlement',()=>{const f=setup(),s=newDemoStore(f.config,f.storage);s.review('ACORN-KIT',{rating:4,body:'',title:'',author:''});s.withdraw('ACORN-KIT');assert.throws(()=>s.review('ACORN-KIT',{rating:5,body:'',title:'',author:''}));});
