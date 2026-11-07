import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import subscriber, {deliver} from '../automation-intake-queue/worker.mjs';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const key = '12'.repeat(32), token = 'x'.repeat(32);
function env(fetch = async () => Response.json({status:'accepted',due_at:200})) {
  return {REVIEWS_ENVIRONMENT:'test', REVIEWS_ORDER_SUBSCRIBER:'test', SOURCE_STORE:'acorn-store',
    SOURCE_ISSUER:'issuer-v1', SOURCE_INSTALLATION:'installation-1', SOURCE_EVENT_KEY:key,
    REVIEWS_AUTOMATION_TOKEN:token, REVIEWS_API:{fetch}};
}
async function message(changes = {}) {
  const m = {version:1,issuer:'issuer-v1',installation:'installation-1',store:'acorn-store',environment:'test',outbox_id:42,...changes};
  const payload=JSON.stringify(['cartray-reviews-order-confirmed-v1',m.issuer,m.installation,m.store,m.environment,m.outbox_id]);
  const k=await crypto.subtle.importKey('raw',Uint8Array.from(key.match(/../g),x=>parseInt(x,16)),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  m.signature=Buffer.from(await crypto.subtle.sign('HMAC',k,new TextEncoder().encode(payload))).toString('hex');
  return m;
}
test('signed message invokes only the scoped private intake and acknowledges its durable receipt', async()=>{
  let calls=0;
  const e=env(async (url,opts)=>{calls++; assert.equal(url,'https://reviews.internal/reviews-internal/v1/automation-intake');
    assert.deepEqual(JSON.parse(opts.body),{outbox_id:42}); assert.equal(opts.headers.authorization,`Bearer ${token}`);
    return Response.json({status:'accepted',due_at:200});});
  const m=await message(); assert.equal(await deliver(m,e),'acknowledged'); assert.equal(await deliver(m,e),'acknowledged'); assert.equal(calls,2);
});
test('forged, cross-scope and malformed hints never reach intake',async()=>{
  let calls=0;const e=env(async()=>{calls++;return Response.json({status:'accepted',due_at:200});});
  const m=await message(); for(const bad of [{...m,outbox_id:43},{...m,store:'other'},{...m,signature:'00'.repeat(32)}]) await assert.rejects(deliver(bad,e),/source authentication failed/);
  for(const bad of [{...m,extra:1},await message({environment:'live'})]) assert.equal(await deliver(bad,e),'discard');
  assert.equal(calls,0);
});
test('pre-activation and late dispositions are acknowledged without authorizing delivery',async()=>{
  for (const status of ['pre_activation','late']) assert.equal(await deliver(await message(),env(async()=>Response.json({status,due_at:200}))), 'acknowledged');
});
test('configuration and intake failures retry, and invalid hints are discarded',async()=>{
  const m=await message();let ack=0,retry=0;
  const q=body=>({body,ack(){ack++},retry(opts){assert.equal(opts.delaySeconds,60);retry++}});
  await subscriber.queue({messages:[q(m),q({...m,signature:'00'.repeat(32)}),q({...m,extra:1})]},env(async()=>new Response('',{status:503})));
  assert.equal(retry,2);assert.equal(ack,1);
  await assert.rejects(deliver(m,{...env(),SOURCE_EVENT_KEY:''}),/key unavailable/);
  await assert.rejects(deliver(m,env(async()=>Response.json({status:'accepted',due_at:'200'}))),/receipt/);
  await assert.rejects(deliver(m,env(async()=>new Response('x'.repeat(4097)))),/too large/);
});
test('a stalled intake is bounded and retried', async()=>{
  const start=Date.now(); await assert.rejects(deliver(await message(),env(()=>new Promise(()=>{}))),/timeout/);
  assert.ok(Date.now()-start < 3500);
});
test('accepted purchases record optional checkout permission; absent permission is acknowledged, outages retry',async()=>{
  let evidenceCalls=0; const e=env();
  Object.assign(e,{AUTOMATION_EVIDENCE_ENABLED:'test',AUTOMATION_EVIDENCE_TOKEN:'z'.repeat(32),PURCHASE_EVIDENCE:{fetch:async(url,opts)=>{
    evidenceCalls++; assert.equal(url,'https://purchase-evidence.internal/automation-record');
    assert.deepEqual(JSON.parse(opts.body),{outbox_id:42}); assert.equal(opts.headers.authorization,'Bearer '+e.AUTOMATION_EVIDENCE_TOKEN);
    return Response.json({status:'not_granted'});
  }}});
  const m=await message(); assert.equal(await deliver(m,e),'acknowledged'); assert.equal(evidenceCalls,1);
  e.PURCHASE_EVIDENCE.fetch=async()=>Response.json({status:'recorded'}); assert.equal(await deliver(m,e),'acknowledged');
  e.PURCHASE_EVIDENCE.fetch=async()=>new Response(null,{status:503}); await assert.rejects(deliver(m,e),/evidence unavailable/);
  e.PURCHASE_EVIDENCE.fetch=async()=>Response.json({status:'unexpected'}); await assert.rejects(deliver(m,e),/receipt/);
  e.REVIEWS_API.fetch=async()=>Response.json({status:'pre_activation',due_at:200});
  e.PURCHASE_EVIDENCE.fetch=async()=>{throw Error('must not run')}; assert.equal(await deliver(m,e),'acknowledged');
});
test('purchase evidence has its own bounded deadline after intake',async()=>{
  const e=env(); Object.assign(e,{AUTOMATION_EVIDENCE_ENABLED:'test',AUTOMATION_EVIDENCE_TOKEN:'z'.repeat(32),
    PURCHASE_EVIDENCE:{fetch:()=>new Promise(resolve=>setTimeout(()=>resolve(Response.json({status:'recorded'})),2100))}});
  assert.equal(await deliver(await message(),e),'acknowledged');
});
test('due scheduling requires recorded permission and acknowledges only durable queue acceptance', async()=>{
  const e=env(); let state='recorded', calls=0;
  Object.assign(e,{AUTOMATION_EVIDENCE_ENABLED:'test',AUTOMATION_EVIDENCE_TOKEN:'z'.repeat(32),
    PURCHASE_EVIDENCE:{fetch:async()=>Response.json({status:state})},
    AUTOMATION_DUE_SCHEDULING_ENABLED:'test',DUE_SCHEDULER_TOKEN:'d'.repeat(32),
    DUE_SCHEDULER:{fetch:async(url,opts)=>{calls++;assert.equal(url,'https://due-scheduler.internal/schedule');
      assert.equal(opts.headers.authorization,'Bearer '+e.DUE_SCHEDULER_TOKEN);
      assert.deepEqual(JSON.parse(opts.body),{outbox_id:42}); return Response.json({status:'scheduled'});}}});
  const m=await message();
  assert.equal(await deliver(m,e),'acknowledged'); assert.equal(calls,1);
  state='not_granted'; assert.equal(await deliver(m,e),'acknowledged'); assert.equal(calls,1);
  state='recorded'; e.DUE_SCHEDULER.fetch=async()=>new Response(null,{status:503});
  await assert.rejects(deliver(m,e),/scheduling unavailable/);
  e.DUE_SCHEDULER.fetch=async()=>Response.json({status:'not_accepted'});
  await assert.rejects(deliver(m,e),/scheduling receipt/);
  e.AUTOMATION_EVIDENCE_ENABLED='disabled'; await assert.rejects(deliver(m,e),/scheduling unavailable/);
});
