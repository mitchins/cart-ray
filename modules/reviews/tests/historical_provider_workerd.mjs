/** Synthetic SMTP2GO boundary linked to the actual Python Reviews Workerd endpoint. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Miniflare } from 'miniflare';
const [base, historyToken, candidate] = process.argv.slice(2);
const generation = 'a'.repeat(64), delivery = 'b'.repeat(64), now = Math.floor(Date.now()/1000);
const source = readFileSync(new URL('../provider-reconciliation/worker.mjs', import.meta.url), 'utf8');
let providerCalls = 0, recipientCalls = 0;
const mockProvider = {type: 'fetcher', handler: async request => {
  assert.equal(new URL(request.url).pathname, '/v3/activity/search'); providerCalls++;
  const input = await request.json(); assert.equal(input.search_recipient, 'synthetic@example.test');
  return Response.json({data:{total_events:1,events:[{to:'synthetic@example.test',email_id:'synthetic_id',
    event:'delivered',date:new Date().toISOString(),custom_headers:{'X-Reviews-Delivery-Identity':delivery}}]}});
}};
const recipient = {type:'fetcher', handler: async request => {
  assert.equal(new URL(request.url).pathname, '/historical-recipient');
  assert.equal(request.headers.get('Authorization'), 'Bearer ' + 'h'.repeat(40)); recipientCalls++;
  const body = await request.json();
  const response = await fetch(base + '/reviews-internal/v1/historical-recipient-match', {
    method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+historyToken},
    body:JSON.stringify({...body,candidate}),
  });
  if (!response.ok || (await response.json()).status !== 'matched') return new Response('',{status:503});
  return Response.json({email:'synthetic@example.test'});
}};
const mf = new Miniflare({workers:[{config:{name:'provider-reconciliation',type:'worker',compatibilityDate:'2026-08-31',
  manifest:{mainModule:'worker.mjs',modules:{'worker.mjs':{type:'esm',contents:source}}},env:{
    REVIEWS_DB:{type:'d1',id:'synthetic-history'},REVIEWS_ENVIRONMENT:{type:'json',value:'test'},
    SMTP2GO_RECONCILIATION:{type:'json',value:'test'},REVIEWS_STORE:{type:'json',value:'example-store'},
    SMTP2GO_ACTIVITY_KEY:{type:'json',value:'api-synthetic'},RECONCILIATION_TOKEN:{type:'json',value:'z'.repeat(40)},
    HISTORICAL_RECIPIENT_TOKEN:{type:'json',value:'h'.repeat(40)},APPROVED_TEST_EMAIL:{type:'json',value:'synthetic@example.test'},
    PURCHASE_EVIDENCE:recipient}},dev:{outboundService:mockProvider}}]});
try {
  const db=await mf.getD1Database('REVIEWS_DB');
  await db.exec('CREATE TABLE invitation_generations(store_key TEXT,environment TEXT,acquisition_id TEXT,generation_id TEXT,delivery_identity TEXT,state TEXT,prepared_at INTEGER,request_key TEXT,PRIMARY KEY(store_key,environment,acquisition_id,generation_id)); CREATE TABLE review_automation_intents(store_key TEXT,environment TEXT,acquisition_id TEXT,outbox_id INTEGER);');
  const migration=readFileSync(new URL('../migrations/0031_provider_delivery_receipts.sql',import.meta.url),'utf8');
  await db.batch(migration.match(/CREATE TABLE[\s\S]+?\);/g).map(sql=>db.prepare(sql)));
  await db.prepare('INSERT INTO invitation_generations VALUES (?,?,?,?,?,?,?,?)').bind('example-store','test','order_1',generation,delivery,'uncertain',now-10,'initial_10').run();
  await db.prepare('INSERT INTO review_automation_intents VALUES (?,?,?,?)').bind('example-store','test','order_1',10).run();
  const request={acquisition_id:'order_1',generation_id:generation,delivery_identity:delivery};
  const response=await mf.dispatchFetch('https://private/discover',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+'z'.repeat(40)},body:JSON.stringify(request)});
  assert.equal(response.status,200);assert.deepEqual(await response.json(),{status:'matched_needs_operator',events:['delivered']});
  assert.equal(providerCalls,1);assert.equal(recipientCalls,1);
  assert.equal((await db.prepare('SELECT count(*) n FROM review_provider_identity_discoveries').first()).n,1);
  assert.equal((await db.prepare('SELECT state FROM invitation_generations').first()).state,'uncertain');
} finally { await mf.dispose(); }
