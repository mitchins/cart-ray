import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewsSummary,createSummaryReader,observeSummaryInvalidation,reviewSummaryText} from '../modules/reviews/ui/public.mjs';
const summary=id=>({subject_id:id,snapshot_id:'native-1',rating:{average:4,rated_count:1},contribution_count:2});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('summary only uses validated public reads without credentials or contributions',async()=>{
 const calls=[];const value=await reviewsSummary('one',async(url,options)=>{calls.push({url,options});return Response.json(summary('one'));});
 assert.equal(value.rating.rated_count,1);assert.equal(calls.length,1);assert.match(calls[0].url,/\/summary$/);
 assert.equal(calls[0].options.credentials,'omit');assert.equal(calls[0].options.cache,'no-store');
 await assert.rejects(reviewsSummary(123));
 for(const value of [{...summary('one'),subject_id:'other'}, {...summary('one'),contribution_count:0},
  {...summary('one'),rating:{average:0,rated_count:1}}, {...summary('one'),rating:{average:4,rated_count:0}}])
   await assert.rejects(reviewsSummary('one',async()=>Response.json(value)));
 const empty=await reviewsSummary('one',async()=>Response.json({...summary('one'),rating:{average:null,rated_count:0},contribution_count:1}));
 assert.equal(empty.rating.average,null);
});
test('two concurrent reads, deduplicated pending and settled results, stale reread, immutable caller copies',async()=>{
 let clock=0,active=0,peak=0,calls=0;const pending=[];
 const reader=createSummaryReader({now:()=>clock,fetchImpl:(url)=>new Promise(resolve=>{
  calls++;active++;peak=Math.max(active,peak);pending.push(()=>{active--;resolve(Response.json(summary(url.split('/').at(-2))));});
 })});
 const a=reader.read('one'),b=reader.read('one'),c=reader.read('two'),d=reader.read('three');
 assert.equal(calls,2);pending.shift()();await tick();assert.equal(calls,3);assert.equal(peak,2);
 pending.shift()();pending.shift()();const [first]=await Promise.all([a,b,c,d]);
 first.rating.average=1;assert.equal((await reader.read('one')).rating.average,4);assert.equal(calls,3);
 clock=30000;const stale=reader.read('one');assert.equal(calls,4);pending.shift()();await stale;
 reader.dispose();await assert.rejects(reader.read('one'));
});
test('invalidated late responses cannot settle or cache, and queued aborts do not consume network',async()=>{
 const pending=[];let calls=0;
 const reader=createSummaryReader({fetchImpl:url=>new Promise(resolve=>{calls++;pending.push(()=>resolve(Response.json(summary(url.split('/').at(-2)))));})});
 const first=reader.read('one');const denied=assert.rejects(first);reader.invalidate('one');pending.shift()();await denied;await tick();
 const a=reader.read('a'),b=reader.read('b');const controller=new AbortController();
 const queued=reader.read('queued',{signal:controller.signal});const aborted=assert.rejects(queued);controller.abort();await aborted;
 pending.shift()();pending.shift()();await Promise.all([a,b]);assert.equal(calls,3);
 reader.dispose();
});
test('aborting one shared subscriber preserves the other',async()=>{
 let finish;let calls=0;const reader=createSummaryReader({fetchImpl:()=>{calls++;return new Promise(resolve=>{finish=()=>resolve(Response.json(summary('one')));});}});
 const controller=new AbortController();const a=reader.read('one',{signal:controller.signal}),b=reader.read('one');
 const rejected=assert.rejects(a);controller.abort();await rejected;finish();assert.equal((await b).subject_id,'one');assert.equal(calls,1);reader.dispose();
});
test('rolling admission and shared429 Retry-After survive invalidation without turning failure into zero',async()=>{
 let clock=0,calls=0;const reader=createSummaryReader({now:()=>clock,fetchImpl:async url=>{calls++;return Response.json(summary(url.split('/').at(-2)));}});
 for(let i=0;i<30;i++)await reader.read('s'+i);
 await assert.rejects(reader.read('blocked'));reader.invalidate();await assert.rejects(reader.read('still-blocked'));assert.equal(calls,30);
 clock=60001;await reader.read('allowed');assert.equal(calls,31);reader.dispose();
 calls=0;const limited=createSummaryReader({now:()=>clock,fetchImpl:async url=>{calls++;return calls===1?new Response('',{status:429,headers:{'Retry-After':'120'}}):Response.json(summary(url.split('/').at(-2)));}});
 await assert.rejects(limited.read('one'));limited.invalidate();clock+=60000;await assert.rejects(limited.read('two'));assert.equal(calls,1);
 clock+=60001;await limited.read('two');assert.equal(calls,2);limited.dispose();
});
test('lifecycle invalidates known changes/navigation and stale restoration, with removable listeners',async()=>{
 const target=new EventTarget(),doc=new EventTarget();doc.visibilityState='visible';const log=[];let stale=false;
 const reader={invalidate:id=>log.push(['invalidate',id]),isStale:()=>stale};
 const stop=observeSummaryInvalidation(reader,event=>log.push(['callback',event.reason]),{target,doc});
 target.dispatchEvent(new Event('focus'));assert.equal(log.length,0);stale=true;target.dispatchEvent(new Event('focus'));
 assert.deepEqual(log.slice(0,2),[['invalidate',undefined],['callback','restoration']]);
 const event=new Event('cartray:reviews-changed');event.detail={subjectId:'one'};doc.dispatchEvent(event);
 assert.deepEqual(log.slice(-2),[['invalidate','one'],['callback','change']]);stop();const size=log.length;
 target.dispatchEvent(new Event('popstate'));assert.equal(log.length,size);
});

test('rated and unrated summary text has honest independent singular counts',()=>{
 assert.equal(reviewSummaryText({...summary('one'),contribution_count:1}),'4 out of 5 · 1 rating · 1 contribution');
 assert.equal(reviewSummaryText({...summary('one'),rating:{rated_count:0,average:null},contribution_count:1}),
   'No ratings yet · 1 contribution');
});
